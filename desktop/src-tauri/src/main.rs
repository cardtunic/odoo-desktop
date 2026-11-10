#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs::{self, OpenOptions},
    net::TcpListener,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{Mutex, atomic::{AtomicBool, AtomicU16, Ordering}},
    thread,
    time::{Duration, Instant},
};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;
mod backups;
mod downloads;
mod file_dialog;

#[derive(Default)]
struct Backend {
    child: Mutex<Option<Child>>,
    stopping: AtomicBool,
    port: AtomicU16,
}

fn config_path(app: &tauri::AppHandle) -> Result<PathBuf, Box<dyn std::error::Error>> {
    if let Some(path) = std::env::var_os("ODOO_DESKTOP_CONFIG") {
        let path = PathBuf::from(path);
        if !path.is_file() {
            return Err(format!("Configuration not found: {}", path.display()).into());
        }
        return Ok(path.canonicalize()?);
    }
    if cfg!(debug_assertions) {
        return Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../odoo.conf").canonicalize()?);
    }
    let path = app.path().app_config_dir()?.join("odoo.conf");
    if !path.is_file() {
        fs::create_dir_all(path.parent().unwrap())?;
        // Never copy credentials into the application bundle.
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        use std::io::Write;
        options.open(&path)?.write_all(include_bytes!("../../odoo.conf.example"))?;
        return Err(format!("Configure your database in {}, then reopen Odoo Desktop. No database is initialized automatically.", path.display()).into());
    }
    Ok(path)
}

fn start_backend(app: &tauri::AppHandle) -> Result<String, Box<dyn std::error::Error>> {
    let config = config_path(app)?;
    let data = app.path().app_data_dir()?;
    fs::create_dir_all(&data)?;
    let log_path = data.join("odoo.log");
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let log = options.open(&log_path)?;
    let port = TcpListener::bind("127.0.0.1:0")?.local_addr()?.port();
    // Report renderers fetch CSS/images outside the browser. A URL left over
    // from a previous random port produces unstyled PDFs or missing images.
    let mut report = if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut cmd = Command::new("uv");
        cmd.args(["run", "--no-sync", "python"]).arg(root.join("desktop/scripts/configure_report.py")).current_dir(root);
        cmd
    } else {
        let resources = app.path().resource_dir()?;
        let runtime = resources.join("runtime");
        let mut cmd = Command::new(runtime.join("python/bin/python3"));
        cmd.env("PYTHONHOME", runtime.join("python")).env_remove("PYTHONPATH");
        cmd.arg(resources.join("configure_report.py"));
        cmd
    };
    let status = report.arg(&config).arg(format!("http://127.0.0.1:{port}"))
        .stdout(Stdio::from(log.try_clone()?)).stderr(Stdio::from(log.try_clone()?)).status()?;
    if !status.success() { return Err(format!("Could not configure PDF report resources. See {}", log_path.display()).into()); }
    let mut command = if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut cmd = Command::new("uv");
        cmd.arg("run").arg("--no-sync").arg("python").arg("-m").arg("odoo").current_dir(root);
        cmd
    } else {
        let runtime = app.path().resource_dir()?.join("runtime");
        let mut cmd = Command::new(runtime.join("python/bin/python3"));
        cmd.arg("-m").arg("odoo").current_dir(runtime.join("app"));
        // AppRun sets PYTHONHOME to AppDir/usr, but our interpreter and
        // standard library live together in this nested runtime directory.
        cmd.env("PYTHONHOME", runtime.join("python"));
        cmd.env("PYTHONPATH", runtime.join("app"));
        cmd
    };
    command
        .arg("-c").arg(config)
        .arg("--http-interface=127.0.0.1")
        .arg(format!("--http-port={port}"))
        .arg("--workers=0")
        .arg("--no-database-list")
        .env("PYTHONNOUSERSITE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    {
        let state = app.state::<Backend>();
        let mut child = state.child.lock().unwrap();
        if state.stopping.load(Ordering::SeqCst) {
            return Err("Startup cancelled".into());
        }
        state.port.store(port, Ordering::SeqCst);
        *child = Some(command.spawn()?);
    }
    let url = format!("http://127.0.0.1:{port}");
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(3))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let deadline = Instant::now() + Duration::from_secs(300);
    while Instant::now() < deadline {
        {
            let state = app.state::<Backend>();
            let mut guard = state.child.lock().unwrap();
            if state.stopping.load(Ordering::SeqCst) {
                return Err("Startup cancelled".into());
            }
            let Some(child) = guard.as_mut() else { return Err("Startup cancelled".into()); };
            if let Some(status) = child.try_wait()? {
                return Err(format!("Odoo exited ({status}). See {}", log_path.display()).into());
            }
        }
        if let Ok(response) = client.get(format!("{url}/web/login")).send() {
            if response.status().is_success() || response.status().is_redirection() {
                return Ok(url);
            }
        }
        thread::sleep(Duration::from_millis(500));
    }
    Err(format!("Odoo did not become ready within 5 minutes. Check your connection, database initialization, and {}", log_path.display()).into())
}

fn stop_backend(app: &tauri::AppHandle) {
    let state = app.state::<Backend>();
    state.stopping.store(true, Ordering::SeqCst);
    let child = state.child.lock().unwrap().take();
    if let Some(mut child) = child {
        if matches!(child.try_wait(), Ok(Some(_))) { return; }
        #[cfg(unix)]
        unsafe { libc::kill(child.id() as i32, libc::SIGTERM); }
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) { return; }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn main() {
    // Request portals for GTK/WebKit native file controls as well. Set this
    // before any GTK threads/windows exist; it affects this app, not the session.
    #[cfg(target_os = "linux")]
    std::env::set_var("GTK_USE_PORTAL", "1");
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(Backend::default())
        .manage(backups::Backups::default())
        .invoke_handler(tauri::generate_handler![backups::backup_settings, backups::configure_google, backups::choose_backup_zip, backups::backup_status, backups::cancel_google_signin, backups::run_backup])
        .menu(|app| {
            use tauri::menu::{Menu, Submenu, MenuItem};
            let backup = MenuItem::with_id(app, "drive-backup", "Back up to Google Drive…", true, None::<&str>)?;
            let import = MenuItem::with_id(app, "zip-import", "Import backup ZIP…", true, None::<&str>)?;
            let data = Submenu::with_items(app, "Data", true, &[&backup, &import])?;
            Menu::with_items(app, &[&data])
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "drive-backup" | "zip-import" => backups::open(app),
            _ => (),
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle();
                if backups::active(app) {
                    backups::cancel_signin(app);
                    api.prevent_close();
                    if let Some(transfer) = app.get_webview_window("transfer") { let _ = transfer.set_focus(); }
                } else if window.label() == "main" {
                    stop_backend(app);
                    if let Some(transfer) = app.get_webview_window("transfer") { let _ = transfer.close(); }
                }
            }
        })
        .setup(|app| {
            let navigation = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Odoo Desktop")
                .inner_size(1280.0, 840.0)
                .min_inner_size(800.0, 600.0)
                .on_navigation(move |url| {
                    let port = navigation.state::<Backend>().port.load(Ordering::SeqCst);
                    let startup = (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
                        || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"));
                    startup || downloads::same_origin(url, port)
                })
                .on_download(downloads::handle)
                .build()?;
            let handle = app.handle().clone();
            thread::spawn(move || {
                let result = start_backend(&handle).and_then(|url| {
                    let target: tauri::Url = url.parse()?;
                    if handle.state::<Backend>().stopping.load(Ordering::SeqCst) { return Ok(()); }
                    if let Some(window) = handle.get_webview_window("main") {
                        window.navigate(target)?;
                    }
                    Ok(())
                });
                if let Err(error) = result {
                    if handle.state::<Backend>().stopping.load(Ordering::SeqCst) { return; }
                    handle.dialog().message(error.to_string()).title("Odoo Desktop could not start").blocking_show();
                    handle.exit(1);
                    return;
                }
                // Do not leave a dead desktop window if the backend later exits.
                loop {
                    thread::sleep(Duration::from_secs(2));
                    let status = {
                        let state = handle.state::<Backend>();
                        let mut guard = state.child.lock().unwrap();
                        let Some(child) = guard.as_mut() else { return; };
                        child.try_wait()
                    };
                    if let Ok(Some(status)) = status {
                        handle.dialog().message(format!("Odoo stopped ({status}). Check odoo.log in the application data directory."))
                            .title("Odoo Desktop stopped").blocking_show();
                        handle.exit(1);
                        return;
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Could not create Odoo Desktop");
    app.run(|app, event| {
        if matches!(event, tauri::RunEvent::ExitRequested { .. }) {
            app.state::<Backend>().stopping.store(true, Ordering::SeqCst);
        }
        if matches!(event, tauri::RunEvent::Exit) {
            stop_backend(app);
        }
    });
}
