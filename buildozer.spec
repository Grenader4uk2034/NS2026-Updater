[app]
title = NS2026 Updater
package.name = ns2026updater
package.domain = org.ns2026
source.dir = app
source.include_exts = py,png,jpg,kv,atlas,json
version = 0.1
requirements = python3,kivy
orientation = portrait
fullscreen = 0

android.api = 35
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.permissions = INTERNET
android.private_storage = True

# Use python-for-android fix for corrupted temporary pip venv
p4a.branch = develop
p4a.commit = 0382d27

[buildozer]
log_level = 2
warn_on_root = 1
