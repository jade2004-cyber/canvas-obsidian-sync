# Changelog

## 0.2.2

- Retry temporary DNS lookup failures instead of exiting immediately
- Extend network recovery attempts so a sync can continue after macOS dark wake
- Show the actual final error in status and failure notifications

## 0.2.1

- Add an optional layout mode that syncs directly into existing course folders
- Preserve existing file paths and layout settings during reconfiguration
- Avoid archiving a file while another current Canvas ID still references it
- Keep the original `Canvas/` layout as the backward-compatible default

## 0.2.0

- Add double-click install, update, configure, status, diagnostics, sync-now, and uninstall commands
- Add Chinese installer prompts and common error guidance
- Show the last scheduled result and next planned synchronization time
- Preserve existing course settings during reconfiguration
- Validate candidate settings before replacing a working configuration
- Record scheduled synchronization results without storing the Canvas token

## 0.1.0

- Initial read-only incremental Canvas-to-Obsidian synchronization
- Add Keychain token storage, LaunchAgent scheduling, safe archival, retries, and notifications
