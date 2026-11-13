use std::{fs, path::PathBuf, process::{Child, Command, Stdio}, sync::Mutex, thread, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
use tauri::Manager;

#[derive(Default)]
pub struct Database {
    child: Mutex<Option<Child>>,
}

pub fn runtime(app: &tauri::AppHandle) -> Result<PathBuf, Box<dyn std::error::Error>> {
    Ok(if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("runtime")
    } else {
        app.path().resource_dir()?.join("runtime")
    })
}

pub fn environment(app: &tauri::AppHandle, command: &mut Command) -> Result<(), Box<dyn std::error::Error>> {
    // Odoo and helper processes must not survive an abruptly killed desktop.
    // Only async-signal-safe syscalls run between fork and exec (GTK is threaded).
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        let parent = std::process::id() as libc::pid_t;
        unsafe {
            command.pre_exec(move || {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getppid() != parent {
                    libc::kill(libc::getpid(), libc::SIGTERM);
                }
                Ok(())
            });
        }
    }
    let native = runtime(app)?.join("native");
    if !native.join("bin").is_dir() { return Ok(()); }
    let mut paths = vec![native.join("bin")];
    paths.extend(std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()));
    command.env("PATH", std::env::join_paths(paths)?);
    command.env("ODOO_DESKTOP_NATIVE_BIN", native.join("bin"));
    let mut libraries = vec![native.join("lib"), native.join("postgres/lib")];
    libraries.extend(std::env::split_paths(&std::env::var_os("LD_LIBRARY_PATH").unwrap_or_default()));
    command.env("LD_LIBRARY_PATH", std::env::join_paths(libraries)?);
    command.env("MAGIC", native.join("share/misc/magic.mgc"));
    Ok(())
}

// Start once per app, not once per Odoo restart. The backup worker pauses only
// Odoo; its database stays available until the whole desktop exits.
pub fn start(app: &tauri::AppHandle, config: &std::path::Path, data: &std::path::Path, log: &fs::File) -> Result<(), Box<dyn std::error::Error>> {
    let state = app.state::<Database>();
    let mut guard = state.child.lock().unwrap();
    if let Some(child) = guard.as_mut() {
        if child.try_wait()?.is_some() { return Err("The local database service stopped. Close the app and check odoo.log.".into()); }
        return Ok(());
    }
    let ready = data.join(format!(".database-ready-{}-{}.json", std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos()));
    let runtime = runtime(app)?;
    let mut command = if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut cmd = Command::new("uv");
        cmd.args(["run", "--no-sync", "python"]).arg(root.join("desktop/scripts/managed_database.py")).current_dir(root);
        cmd
    } else {
        let mut cmd = Command::new(runtime.join("python/bin/python3"));
        cmd.env("PYTHONHOME", runtime.join("python")).env_remove("PYTHONPATH");
        cmd.arg(app.path().resource_dir()?.join("managed_database.py"));
        cmd
    };
    environment(app, &mut command)?;
    command.arg(&runtime).arg(config).arg(data).arg(&ready).arg(std::process::id().to_string())
        .env("PYTHONNOUSERSITE", "1").stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone()?)).stderr(Stdio::from(log.try_clone()?));
    if app.state::<super::Backend>().stopping.load(std::sync::atomic::Ordering::SeqCst) {
        return Err("Startup cancelled".into());
    }
    *guard = Some(command.spawn()?);
    // Release this lock between readiness checks so closing during first-run
    // initialization can stop the service immediately rather than block the UI.
    drop(guard);
    let result = (|| -> Result<Option<String>, Box<dyn std::error::Error>> {
        let deadline = Instant::now() + Duration::from_secs(900);
        loop {
            if app.state::<super::Backend>().stopping.load(std::sync::atomic::Ordering::SeqCst) {
                return Err("Startup cancelled".into());
            }
            if let Ok(bytes) = fs::read(&ready) {
                let value: serde_json::Value = serde_json::from_slice(&bytes)?;
                if value["ready"].as_bool() == Some(true) {
                    return Ok(value["first_login"].as_str().map(str::to_owned));
                }
            }
            if !running(app)? { return Err("Local database setup stopped. Check odoo.log. Existing data has not been replaced.".into()); }
            if Instant::now() > deadline { return Err("Local database setup timed out. Check odoo.log before restarting.".into()); }
            thread::sleep(Duration::from_millis(100));
        }
    })();
    let _ = fs::remove_file(&ready);
    if let Some(message) = result? {
        use tauri_plugin_dialog::DialogExt;
        app.dialog().message(message).title("Your private local Odoo database is ready").blocking_show();
        let _ = fs::remove_file(config.with_file_name("first-login.pending"));
    }
    Ok(())
}

pub fn running(app: &tauri::AppHandle) -> Result<bool, std::io::Error> {
    let state = app.state::<Database>();
    let mut guard = state.child.lock().unwrap();
    match guard.as_mut() {
        Some(child) => Ok(child.try_wait()?.is_none()),
        None => Ok(false),
    }
}

pub fn stop(app: &tauri::AppHandle) {
    let state = app.state::<Database>();
    let child = state.child.lock().unwrap().take();
    if let Some(mut child) = child {
        if matches!(child.try_wait(), Ok(Some(_))) { return; }
        #[cfg(unix)]
        unsafe { libc::kill(child.id() as i32, libc::SIGTERM); }
        // Allow PostgreSQL fast shutdown and WAL flushing before resorting to a
        // supervisor kill. Its PostgreSQL child also has a parent-death signal.
        let deadline = Instant::now() + Duration::from_secs(45);
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) { return; }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}
