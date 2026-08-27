# LocalHBT for Android

The phone app. It is a shell around the translator's own web UI, so every screen
and every button comes from `index.html` on the PC — there is only one interface
and the phone displays it. What the shell adds is the handful of things a browser
tab cannot do: writing files into a folder you picked on the phone, remembering
which machine to talk to, keeping the screen awake during a long job, and
showing something useful instead of a Chrome error page when the PC is asleep.

```
Phone  100.98.198.75                  PC  100.104.64.110
┌──────────────────────┐  Tailscale   ┌────────────────────────┐
│ LocalHBT.apk         │◄────────────►│ server.py  :8770       │
│  ├ WebView → the UI  │  HTTP + JSON │  ├ Ollama  :11434      │
│  ├ IndexedDB cache   │              │  ├ Translations/*.json │
│  └ SAF file writer   │              │  └ Translated/*.txt    │
└──────────────────────┘              └────────────────────────┘
```

## Build it

    Build APK.bat

That runs `build.ps1`, which drives the SDK tools directly rather than through
Gradle:

    aapt2 compile → aapt2 link → javac → d8 → aapt add → zipalign → apksigner

A one-Activity WebView app has no library dependencies, so there is nothing for a
dependency resolver to resolve. Skipping Gradle means the build needs no network,
no daemon, and no version alignment between AGP, Gradle and Kotlin — it uses what
Android Studio already installed. It takes a few seconds.

The result is `build\LocalHBT.apk`, signed with `debug.keystore` (created on the
first build, then reused — keep it, since Android will refuse to *upgrade* an
installed app if a later APK is signed with a different key).

Requires: the Android SDK with any build-tools and a platform (both already
present via Android Studio), and a JDK 17+ — the one inside Android Studio's
`jbr` folder is found automatically.

## Put it on the phone

**Over WiFi, no cable ever.** Android 11+ can pair with a code instead:

    Install over WiFi.bat

On the phone, Settings → Developer options → **Wireless debugging** ON. The first
run asks for the pairing address and six-digit code from *Pair device with
pairing code*; after that it only needs the address from the main Wireless
debugging screen — a different port, which is the usual thing to get wrong. The
address is remembered, so later runs try it first and go straight to installing.

Same WiFi as the PC: use the address the phone shows. Different networks: use the
phone's Tailscale address, `100.98.198.75`, with that same port.

The older `adb tcpip 5555` route is not used here — it needs a USB connection to
put the phone into network mode in the first place, which rather defeats the
point.

**With a cable.** Turn on Developer options → USB debugging, plug the phone in,
then:

    Install to phone.bat

**Neither.** Copy `build\LocalHBT.apk` to the phone however you like and tap it.
Android will ask you to allow installing from that app the first time.

## First run

The app asks for the computer's address, pre-filled with
`http://100.104.64.110:8770`. That is this PC's Tailscale IP and the translator's
port. Change it here if either ever changes; the ⚙ button in the app's header
brings this dialog back later.

For **Save to device**, the first press asks which folder to write into. Whatever
you pick — Downloads, a Drive folder, an SD card — is remembered, and every
later save writes there without asking. Each document becomes a folder
containing `source.txt`, `translation.txt` and `side-by-side.txt`.

## Reading mode

The menu button at the top right slides a panel in from the right; **Reading
mode** is in it. It shows the translation with the editor removed, keeps a
bookmark per book on the device, and restores it when you come back. Back closes
the menu, then the reader, before it leaves the app.

This lives in `index.html`, not in the APK, so it arrives by restarting the
server - no rebuild.

## What it needs from the PC

`server.py` must be running (`Run Translator.bat`) and Tailscale must be
connected on both devices. The PC has to be awake — translation happens in
Ollama on the PC, and no phone app can change that.

The server now listens on every interface but answers only loopback and
`100.64.0.0/10`, the range Tailscale allocates from. Joining a café's WiFi does
not expose the port to that network.

## Working offline

Every document the phone opens is copied into IndexedDB inside the app. With the
PC asleep you can still read and edit the source and the full translation text;
edits are held on the phone and pushed when the machine comes back. The
chunk-by-chunk **Live** view is the one thing that needs a connection — it is a
view of work in progress, which needs the machine doing the work.

If both copies changed since the last sync, the push is refused and the app asks
which one to keep rather than picking silently.

## Files

| Path | What it is |
|---|---|
| `app/java/.../MainActivity.java` | The whole app — WebView, JS bridge, SAF writer |
| `app/AndroidManifest.xml` | Permissions, launcher activity, cleartext policy |
| `app/res/` | Icon, theme, network security config |
| `build.ps1` | The Gradle-free build |
| `Install over WiFi.bat` | Pair-and-install with no cable |
| `.wireless-target` | Last WiFi address used, so it reconnects by itself |
| `debug.keystore` | Signing key, created on first build — keep it |
| `build/LocalHBT.apk` | The output |

## Notes

- **minSdk 26** (Android 8), **targetSdk 36**.
- Cleartext HTTP is permitted because Tailscale already encrypts the tunnel
  end to end; adding TLS would mean running a certificate authority for one
  machine. The app only ever loads the address you give it.
- To develop this in Android Studio instead, the sources are laid out
  conventionally enough that a `build.gradle.kts` with `minSdk 26` and no
  dependencies will pick them up — point `sourceSets.main` at `app/java` and
  `app/res`.
