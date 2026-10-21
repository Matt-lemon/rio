# Optional Google Home integration

Google Home Android API work must check the [official release notes](https://developers.home.google.com/apis/android/release-notes?hl=ko) for the adopted SDK version and retain that version's dependency, permission and hub requirements. Do not mix requirements from different SDK releases or treat them as the SmartThings schema.

This architecture preview includes Python contracts and a mocked gateway test source, not Android SDK binaries, a tablet build, private bindings or a verified hub/firmware deployment. It does not claim SDK/firmware compliance or actual physical-device control. Permission evidence, transport receipt, reported state and human confirmation remain distinct.

Unverified dependencies, configuration and device capabilities must not be reported as ready. An offline test does not authorize live device commands or changes to an operational deployment.
