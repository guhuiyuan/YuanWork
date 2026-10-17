//! Secure secret storage - uses OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service).

use anyhow::Result;
use keyring::Entry;

const SERVICE_PREFIX: &str = "yuanwork";

/// Get a secret from the OS keyring.
pub fn get_secret(account: &str, key: &str) -> Result<Option<String>> {
    let service = format!("{}-{}", SERVICE_PREFIX, account);
    let entry = Entry::new(&service, key)?;

    match entry.get_password() {
        Ok(password) => Ok(Some(password)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.into()),
    }
}

/// Set a secret in the OS keyring.
pub fn set_secret(account: &str, key: &str, value: &str) -> Result<()> {
    let service = format!("{}-{}", SERVICE_PREFIX, account);
    let entry = Entry::new(&service, key)?;
    entry.set_password(value)?;
    Ok(())
}

/// Delete a secret from the OS keyring.
pub fn delete_secret(account: &str, key: &str) -> Result<()> {
    let service = format!("{}-{}", SERVICE_PREFIX, account);
    let entry = Entry::new(&service, key)?;

    match entry.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(e.into()),
    }
}
