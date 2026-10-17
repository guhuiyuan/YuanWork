//! Filesystem scope - manages which folders the app can access.
//!
//! Tauri v2 requires explicit permission for filesystem access. This module
//! handles the scope configuration.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Get the list of folders the user has granted access to.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct FolderPermission {
    pub path: String,
    pub granted_at: String,
    pub recursive: bool,
}

/// Check if a path is within an authorized folder.
pub fn is_path_allowed(path: &str, allowed: &[FolderPermission]) -> bool {
    let path = PathBuf::from(path);
    for perm in allowed {
        let allowed_path = PathBuf::from(&perm.path);
        if path.starts_with(&allowed_path) {
            return true;
        }
    }
    false
}
