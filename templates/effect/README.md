# {{NAME}}

An insert effect plugin for Akai MPC standalone hardware, by {{VENDOR}}.

_Describe what the plugin does here. This file is included in the distributable zip._

## Build

```
python tools/build.py plugins/{{NAME}}
python tools/package.py plugins/{{NAME}}
```

## Project layout

- `plugin.json` - manifest (name, manufacturer, uid). The `uid` must match the C++ `uniqueId`.
- `src/{{CLASS}}.cpp` - the plugin source (parameters and DSP).
- `ui.json` - which parameters appear on the MPC screen, in which order.
- `build/` - build output (the deployable `{{VENDOR}} - VST - {{NAME}}` folder lives here).
- `dist/` - zips produced by `package.py`.
