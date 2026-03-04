## 2026-03-04 - Version 0.2.0
Bugfix release including some minor improvements.

**Improvements**
- Examples are now included in the package, with script entrypoints for each.
- The `drain` low-level API function is now exposed at the package level
- `wait()` will now acquire frame-local `Cown` objects before shutting down the workers

**Dev Tools**
- Added an internal cown and behavior reference tracking utility

**Bug Fixes**
- Fixed a reference counting bug with cown lists
- Fixed an issue where the boids example did not run on windows due a font
  setting.


## 2026-03-02 - Version 0.1.0
Initial Release.