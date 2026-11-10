use std::{fs::{self, OpenOptions}, io::Write, path::PathBuf, process::{Command, Stdio}, sync::{Mutex, atomic::{AtomicBool, Ordering}}, thread, time::{SystemTime, UNIX_EPOCH}};
use serde_json::{json, Value};
use tauri::{Manager, WebviewWindow, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

#[derive(Default)]
pub struct Backups {
    active: AtomicBool,
    action: Mutex<String>,
    status: Mutex<Option<PathBuf>>,
    archive: Mutex<Option<PathBuf>>,
}

fn trusted(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|e| e.to_string())?;
    if window.label() != "transfer" || !((url.scheme() == "tauri" && url.host_str() == Some("localhost")) ||
        (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"))) {
        return Err("Only the local backup window can access these settings.".into());
    }
    Ok(())
}

fn directory(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path().app_config_dir().map_err(|e| e.to_string())
}

fn local_name(app: &tauri::AppHandle) -> Result<String, String> {
    let config = super::config_path(app).map_err(|e| e.to_string())?;
    fs::read_to_string(config).map_err(|e| e.to_string())?.lines().find_map(|line| {
        let (key, value) = line.split_once('=')?;
        (key.trim() == "db_name").then(|| value.trim().to_owned())
    }).ok_or("Local database name not configured".into())
}

fn idle(app: &tauri::AppHandle) -> Result<(), String> {
    if active(app) { Err("A backup operation is running.".into()) } else { Ok(()) }
}

#[tauri::command]
pub fn backup_settings(window: WebviewWindow, app: tauri::AppHandle) -> Result<Value, String> {
    trusted(&window)?;
    let directory = directory(&app)?;
    let archive = app.state::<Backups>().archive.lock().unwrap().clone();
    Ok(json!({"client_configured":directory.join("google-client.json").exists(),
        "connected":directory.join("google-token.json").exists(), "local_database":local_name(&app)?,
        "archive":archive.map(|p| p.display().to_string())}))
}

#[tauri::command]
pub async fn configure_google(window: WebviewWindow, app: tauri::AppHandle) -> Result<bool, String> {
    trusted(&window)?;
    idle(&app)?;
    let directory = directory(&app)?;
    if directory.join("google-token.json").exists() { return Err("Disconnect Google Drive before changing the OAuth client.".into()); }
    let selection = super::file_dialog::open("Choose Google OAuth Desktop app JSON", "Google OAuth client JSON", "*.json").await?;
    let Some(selection) = selection else { return Ok(false); };
    idle(&app)?;
    if directory.join("google-token.json").exists() { return Err("Disconnect Google Drive before changing the OAuth client.".into()); }
    let path = selection;
    if fs::metadata(&path).map_err(|e| e.to_string())?.len() > 1024 * 1024 { return Err("Client JSON is too large.".into()); }
    let data: Value = serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?).map_err(|_| "Invalid JSON")?;
    let installed = &data["installed"];
    let id = installed["client_id"].as_str().ok_or("Choose OAuth credentials for a Desktop app, not a web app.")?;
    let secret = installed["client_secret"].as_str().ok_or("Client secret is missing.")?;
    if !id.ends_with(".apps.googleusercontent.com") || secret.is_empty() { return Err("Invalid Google Desktop client credentials.".into()); }
    fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
    let temporary = directory.join("google-client.tmp");
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)] {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let client = json!({"installed":{"client_id":id,"client_secret":secret}}).to_string();
    options.open(&temporary).and_then(|mut file| file.write_all(client.as_bytes())).map_err(|e| e.to_string())?;
    fs::rename(temporary, directory.join("google-client.json")).map_err(|e| e.to_string())?;
    Ok(true)
}

#[tauri::command]
pub async fn choose_backup_zip(window: WebviewWindow, app: tauri::AppHandle) -> Result<bool, String> {
    trusted(&window)?;
    idle(&app)?;
    let selection = super::file_dialog::open("Choose Odoo Desktop backup ZIP", "Odoo Desktop backup ZIP", "*.zip").await?;
    let Some(selection) = selection else { return Ok(false); };
    idle(&app)?;
    let path = selection.canonicalize().map_err(|e| e.to_string())?;
    if !path.is_file() { return Err("Choose a backup ZIP file.".into()); }
    *app.state::<Backups>().archive.lock().unwrap() = Some(path);
    Ok(true)
}

#[tauri::command]
pub fn backup_status(window: WebviewWindow, app: tauri::AppHandle) -> Result<Value, String> {
    trusted(&window)?;
    let state = app.state::<Backups>();
    let path = state.status.lock().unwrap();
    let mut value = path.as_ref().and_then(|p| fs::read(p).ok()).and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .unwrap_or(json!({"message":"Ready.","done":true,"error":false}));
    value["active"] = json!(state.active.load(Ordering::SeqCst));
    value["action"] = json!(*state.action.lock().unwrap());
    Ok(value)
}

#[tauri::command]
pub fn cancel_google_signin(window: WebviewWindow, app: tauri::AppHandle) -> Result<(), String> {
    trusted(&window)?;
    cancel_signin(&app);
    Ok(())
}

pub fn cancel_signin(app: &tauri::AppHandle) {
    let state = app.state::<Backups>();
    if state.action.lock().unwrap().as_str() == "connect-drive" {
        if let Some(path) = state.status.lock().unwrap().as_ref() {
            let _ = fs::write(path.parent().unwrap().join("cancel"), b"");
        }
    }
}

#[tauri::command]
pub async fn run_backup(window: WebviewWindow, app: tauri::AppHandle, action: String, confirmation: String) -> Result<(), String> {
    trusted(&window)?;
    if !["connect-drive", "disconnect-drive", "drive-backup", "zip-import"].contains(&action.as_str()) { return Err("Unknown backup action.".into()); }
    if app.state::<Backups>().active.swap(true, Ordering::SeqCst) { return Err("A backup operation is already running.".into()); }
    let result = prepare(&app, &action, &confirmation).await;
    if result.is_err() { app.state::<Backups>().active.store(false, Ordering::SeqCst); }
    result
}

async fn prepare(app: &tauri::AppHandle, action: &str, confirmation: &str) -> Result<(), String> {
    let config = super::config_path(app).map_err(|e| e.to_string())?;
    let directory = directory(app)?;
    if action == "connect-drive" && !directory.join("google-client.json").exists() { return Err("Choose a Google OAuth Desktop app client JSON first.".into()); }
    if action == "drive-backup" && !directory.join("google-token.json").exists() { return Err("Connect Google Drive first.".into()); }
    let archive = if action == "zip-import" {
        let name = local_name(app)?;
        if confirmation != name { return Err(format!("Type {name} to confirm local replacement.")); }
        Some(app.state::<Backups>().archive.lock().unwrap().clone().ok_or("Choose a backup ZIP first.")?)
    } else { None };
    if action == "zip-import" || action == "drive-backup" {
        let message = if action == "zip-import" {
            format!("Replace local database {} AND its attachments with the selected ZIP? A complete recovery ZIP is saved first. Only import backups you trust. Odoo will pause; keep the window open until completion.", local_name(app)?)
        } else {
            "Create a ZIP of your database and attachments, and upload it to the connected Google Drive account? Backups contain sensitive business data and are not password-encrypted. Odoo pauses until the operation finishes.".to_owned()
        };
        let handle = app.clone();
        let approved = tauri::async_runtime::spawn_blocking(move || handle.dialog().message(message)
            .title("Confirm backup operation").buttons(MessageDialogButtons::OkCancel).blocking_show()).await.map_err(|e| e.to_string())?;
        if !approved { return Err("Cancelled. No data was changed.".into()); }
    }
    let job = app.path().app_data_dir().map_err(|e| e.to_string())?.join("backups")
        .join(SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis().to_string());
    fs::create_dir_all(&job).map_err(|e| e.to_string())?;
    #[cfg(unix)] {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&job, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    }
    let progress = job.join("status.json");
    fs::write(&progress, json!({"message":"Starting…","done":false,"error":false}).to_string()).map_err(|e| e.to_string())?;
    *app.state::<Backups>().status.lock().unwrap() = Some(progress.clone());
    *app.state::<Backups>().action.lock().unwrap() = action.to_owned();
    let handle = app.clone();
    let action = action.to_owned();
    thread::spawn(move || {
        let paused = action == "zip-import" || action == "drive-backup";
        let result = (|| -> Result<(), String> {
            if paused {
                if let Some(main) = handle.get_webview_window("main") {
                    let _ = main.eval("document.body.innerHTML='<p style=\"font:16px system-ui;padding:32px\">Backup operation in progress. Use the backup window for status.</p>'");
                }
                super::stop_backend(&handle);
            }
            let mut command = if cfg!(debug_assertions) {
                let mut cmd = Command::new("uv");
                cmd.args(["run", "--no-sync", "python"]);
                cmd.arg(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../scripts/backup_drive.py"));
                cmd.current_dir(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
                cmd
            } else {
                let resources = handle.path().resource_dir().map_err(|e| e.to_string())?;
                let runtime = resources.join("runtime");
                let mut cmd = Command::new(runtime.join("python/bin/python3"));
                cmd.env("PYTHONHOME", runtime.join("python")).env_remove("PYTHONPATH");
                cmd.arg(resources.join("backup_drive.py"));
                cmd
            };
            let output = command.arg(&action).arg(config).arg(directory).arg(&job)
                .arg(archive.unwrap_or_default()).env("PYTHONNOUSERSITE", "1").stdin(Stdio::null()).output().map_err(|e| e.to_string())?;
            let finished = fs::read(&progress).ok().and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .and_then(|value| value["done"].as_bool()).unwrap_or(false);
            if !finished { return Err(format!("Backup worker exited ({}) without a completion status. Local files: {}", output.status, job.display())); }
            Ok(())
        })();
        if let Err(error) = result {
            let _ = fs::write(&progress, json!({"message":error,"done":true,"error":true}).to_string());
        }
        let recovery_required = fs::read(&progress).ok().and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .and_then(|value| value["recovery_required"].as_bool()).unwrap_or(false);
        if paused && !recovery_required {
            handle.state::<super::Backend>().stopping.store(false, Ordering::SeqCst);
            match super::start_backend(&handle) {
                Ok(url) => if let Some(window) = handle.get_webview_window("main") { let _ = window.navigate(url.parse().unwrap()); },
                Err(error) => { let _ = fs::write(&progress, json!({"message":format!("Odoo could not restart: {error}. Recovery files: {}", job.display()),"done":true,"error":true}).to_string()); }
            }
        }
        handle.state::<Backups>().active.store(false, Ordering::SeqCst);
    });
    Ok(())
}

pub fn active(app: &tauri::AppHandle) -> bool { app.state::<Backups>().active.load(Ordering::SeqCst) }

pub fn open(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("transfer") { let _ = window.set_focus(); return; }
    let _ = WebviewWindowBuilder::new(app, "transfer", WebviewUrl::App("transfer.html".into()))
        .title("Backups — Odoo Desktop").inner_size(740.0, 880.0).min_inner_size(560.0, 600.0)
        .on_navigation(|url| (url.scheme() == "tauri" && url.host_str() == Some("localhost")) ||
            (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"))).build();
}
