//! File pickers use the desktop portal; message/confirmation dialogs stay on
//! Tauri's existing backend (the portal has no message-dialog interface).
use std::path::PathBuf;
use ashpd::desktop::{file_chooser::{FileFilter, SelectedFiles}, ResponseError};

fn selected(result: ashpd::Result<SelectedFiles>) -> Result<Option<PathBuf>, String> {
    match result {
        Ok(files) => files.uris().first().map(|uri| local_path(uri.as_str())).transpose(),
        Err(ashpd::Error::Response(ResponseError::Cancelled)) => Ok(None),
        Err(error) => Err(format!("The desktop file picker failed: {error}. Check that xdg-desktop-portal and your desktop's portal backend are running.")),
    }
}

fn local_path(value: &str) -> Result<PathBuf, String> {
    let url = tauri::Url::parse(value).map_err(|_| "The desktop picker returned an invalid file URL.".to_string())?;
    url.to_file_path().map_err(|_| "Choose a local file or a locally mounted network location.".into())
}

pub async fn open(title: &str, filter_name: &str, pattern: &str) -> Result<Option<PathBuf>, String> {
    let result = async {
        SelectedFiles::open_file().title(title).modal(true).multiple(false)
            .filter(FileFilter::new(filter_name).glob(pattern))
            .send().await?.response()
    }.await;
    selected(result)
}

pub async fn save(name: &str, directory: Option<PathBuf>) -> Result<Option<PathBuf>, String> {
    let result = async {
        let mut request = SelectedFiles::save_file().title("Save downloaded file")
            .current_name(name).modal(true);
        if let Some(directory) = directory.filter(|path| path.is_dir()) {
            request = request.current_folder(directory)?;
        }
        request.send().await?.response()
    }.await;
    selected(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn decodes_local_filenames_and_rejects_remote_urls() {
        assert_eq!(local_path("file:///home/user/Invoice%202026.pdf").unwrap(), PathBuf::from("/home/user/Invoice 2026.pdf"));
        assert!(local_path("https://example.com/backup.zip").is_err());
    }
    #[test]
    fn cancellation_is_not_an_error() {
        assert!(selected(Err(ashpd::Error::Response(ResponseError::Cancelled))).unwrap().is_none());
    }
}
