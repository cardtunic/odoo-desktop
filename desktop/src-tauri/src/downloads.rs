use std::{fs::{self, OpenOptions}, io, path::{Path, PathBuf}, time::{SystemTime, UNIX_EPOCH}, thread};
use tauri::{Manager, Url, Webview, webview::DownloadEvent};
use tauri_plugin_dialog::DialogExt;

pub fn same_origin(url: &Url, port: u16) -> bool {
    if url.scheme() == "blob" {
        return url.as_str().strip_prefix("blob:").and_then(|value| Url::parse(value).ok())
            .is_some_and(|inner| same_origin(&inner, port));
    }
    port != 0 && url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(port)
}

fn filename(path: &Path) -> String {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let safe: String = name.chars().filter(|c| !c.is_control() && !"/\\:".contains(*c)).take(180).collect();
    if safe.is_empty() || safe == "." || safe == ".." { "download".into() } else { safe }
}

fn cleanup(path: &Path) {
    let _ = fs::remove_file(path);
    if let Some(parent) = path.parent() { let _ = fs::remove_dir(parent); }
}

fn save(source: &Path, target: &Path) -> io::Result<()> {
    if target.canonicalize().ok().as_ref() == Some(&source.canonicalize()?) {
        return Err(io::Error::other("Choose a destination outside the temporary download file"));
    }
    let parent = target.parent().ok_or_else(|| io::Error::other("No destination directory"))?;
    let temporary = parent.join(format!(".odoo-download-{}.tmp", SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)] {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut output = options.open(&temporary)?;
    let result = (|| {
        io::copy(&mut fs::File::open(source)?, &mut output)?;
        output.sync_all()?;
        fs::rename(&temporary, target)
    })();
    if result.is_err() { let _ = fs::remove_file(temporary); }
    result
}

pub fn handle(webview: Webview, event: DownloadEvent<'_>) -> bool {
    let app = webview.app_handle().clone();
    match event {
        DownloadEvent::Requested { url, destination } => {
            let port = app.state::<super::Backend>().port.load(std::sync::atomic::Ordering::SeqCst);
            let allowed = webview.url().ok().is_some_and(|current| same_origin(&current, port)) && same_origin(&url, port);
            if !allowed { return false; }
            let directory = match app.path().app_data_dir() {
                Ok(root) => root.join("downloads").join(SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos().to_string()),
                Err(_) => return false,
            };
            if fs::create_dir_all(&directory).is_err() { return false; }
            #[cfg(unix)] {
                use std::os::unix::fs::PermissionsExt;
                if fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).is_err() { return false; }
            }
            *destination = directory.join(filename(destination));
        }
        DownloadEvent::Finished { path, success, .. } => {
            let Some(source) = path else {
                app.dialog().message("The download did not produce a file. Please try again.").title("Download failed").show(|_| {});
                return true;
            };
            let staged = app.path().app_data_dir().ok().is_some_and(|root| source.starts_with(root.join("downloads")));
            if !staged {
                app.dialog().message(format!("Download {}: {}", if success { "saved to" } else { "failed at" }, source.display()))
                    .title("Download").show(|_| {});
                return true;
            }
            if !success {
                cleanup(&source);
                app.dialog().message("The download failed. Please try again.").title("Download failed").show(|_| {});
                return true;
            }
            let directory = app.path().download_dir().ok();
            tauri::async_runtime::spawn(async move {
                let target = match super::file_dialog::save(&filename(&source), directory).await {
                    Ok(Some(path)) => path,
                    Ok(None) => { cleanup(&source); return; },
                    Err(error) => {
                        app.dialog().message(format!("Cannot save: {error}. Temporary file: {}", source.display())).title("Download not saved").show(|_| {});
                        return;
                    }
                };
                thread::spawn(move || match save(&source, &target) {
                    Ok(()) => {
                        cleanup(&source);
                        app.dialog().message(format!("Saved to:\n{}", target.display())).title("File saved").show(|_| {});
                    }
                    Err(error) => {
                        app.dialog().message(format!("Could not save: {error}\nTemporary file retained at {}", source.display())).title("Download not saved").show(|_| {});
                    }
                });
            });
        }
        _ => (),
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_local_backend_and_its_blobs_are_allowed() {
        assert!(same_origin(&Url::parse("http://127.0.0.1:8001/report/download").unwrap(), 8001));
        assert!(same_origin(&Url::parse("blob:http://127.0.0.1:8001/report-id").unwrap(), 8001));
        assert!(!same_origin(&Url::parse("blob:https://example.com/report-id").unwrap(), 8001));
        assert!(!same_origin(&Url::parse("http://127.0.0.1:8002/report/download").unwrap(), 8001));
        assert!(!same_origin(&Url::parse("file:///etc/passwd").unwrap(), 8001));
    }
    #[test]
    fn safe_download_names() {
        assert_eq!(filename(Path::new("/tmp/Invoice.pdf")), "Invoice.pdf");
        assert_eq!(filename(Path::new("/")), "download");
        assert_eq!(filename(Path::new("bad\nname.pdf")), "badname.pdf");
    }
    #[test]
    fn saves_atomically_and_rejects_overwriting_its_source() {
        let directory = PathBuf::from("/tmp/opencode").join(format!("odoo-download-test-{}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
        fs::create_dir(&directory).unwrap();
        let source = directory.join("source.pdf");
        let target = directory.join("saved.pdf");
        fs::write(&source, b"%PDF-test").unwrap();
        fs::write(&target, b"previous").unwrap();
        save(&source, &target).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"%PDF-test");
        assert!(save(&source, &source).is_err());
        fs::remove_file(source).unwrap();
        fs::remove_file(target).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}
