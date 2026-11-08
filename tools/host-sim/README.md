# host-sim: test your plugin without an MPC

`host_sim` is a tiny command-line VST2 host that behaves like the plugin host
inside the Akai MPC firmware. It loads a **natively built** copy of your plugin
(`.dylib` on macOS, `.so` on Linux, `.dll` on Windows), talks to it in the
same order and with the same quirks as the MPC, renders audio, and prints a
PASS / WARN / FAIL report. `tools/build.py` runs it for you; you can also run
it by hand.

It is one C++17 file with no dependencies beyond the standard library, so it
builds anywhere:

```sh
# with the SDK's zig toolchain (what build.py does)
python -m ziglang c++ -std=c++17 -O2 -o host_sim tools/host-sim/host_sim.cpp

# or with any system compiler
c++ -std=c++17 -O2 -o host_sim tools/host-sim/host_sim.cpp          # macOS / Linux
cl /std:c++17 /O2 /EHsc tools\host-sim\host_sim.cpp                  # Windows (untested)
```

The VST2 ABI comes from the SDK's own `framework/mpc_vst2_abi.h`; no Steinberg
headers are used.

## Commands

### `host_sim <plugin> dump`

Prints `params.json` to stdout (diagnostics go to stderr). `build.py` saves it
as `build/params.json`, and `gen_ui.py` / `validate.py` read it.

```json
{
  "name": "Gain", "vendor": "Acme", "uid": "AcGn", "uidInt": 1097025390, "version": 1000,
  "isInstrument": false, "numInputs": 2, "numOutputs": 2, "numPrograms": 1,
  "programs": ["Default"],
  "params": [
    {"index": 0, "id": null, "name": "Gain", "label": "dB", "display": "0.0", "default": 0.833333,
     "automatable": true, "kind": "float", "choices": []},
    {"index": 1, "id": null, "name": "Mode", "label": "", "display": "Clean", "default": 0.0,
     "automatable": true, "kind": "choice", "choices": ["Clean", "Warm", "Dirty"]},
    {"index": 2, "id": null, "name": "Enable", "label": "", "display": "On", "default": 1.0,
     "automatable": true, "kind": "bool", "choices": ["Off", "On"]}
  ]
}
```

VST2 does not tell a host what *kind* a parameter is, so `kind` and `choices`
are inferred by probing: each parameter is set to `k/(N-1)` for N = 2..16 and
the display text is read back. If some N yields N distinct strings and finer
probing (32, 64 steps) produces exactly the same set, the parameter is
discrete: `bool` when the two strings are On/Off (or Yes/No, True/False...),
`int` when the strings are consecutive integers, otherwise `choice`.
Everything else is `float`, or `int` when every display string is a whole
number. The original value is restored afterwards. This is best effort; a
float with one decimal and a tiny range can look discrete.

`default` is the normalised (0..1) value right after loading, `display` is the
text shown for it, `label` is the unit (`effGetParamLabel`), `automatable` is
`effCanBeAutomated`. `id` is always `null` (VST2 has no parameter ids).

### `host_sim <plugin> test [options]`

```
--seconds N     length of the main render (default 2)
--sr N          sample rate (default 44100, which is what the MPC uses)
--block N       frames per process call (default 128)
--wav out.wav   write the main render as a 16-bit stereo WAV
--notes a,b,c   MIDI notes for instruments (default 60)
--playing       report the transport as playing (default: stopped)
--seed N        seed for the random parameter tests (default 1234)
--verbose       log every opcode in both directions
```

Exit code: `0` pass (warnings allowed), `1` at least one FAIL (or bad
arguments), `2` the plugin crashed (the report says in which phase and which
call).

What `test` does, in order:

1. **Load exactly like the MPC.** `VSTPluginMain`, check `magic == 'VstP'`,
   `resvd2 == 0`, `object != NULL`; then `effIdentify`, `effSetSampleRate`,
   `effSetBlockSize(max(32, block))`, `effOpen`, speaker arrangement and
   input/output pin properties, `effCanBeAutomated` for every parameter,
   `effCanDo "bypass"`; then `effIdentify`, `effSetSampleRate`,
   `effSetBlockSize(max(128, block))`, `effGetVendorString`, **`effOpen` a
   second time**, `effSetProgram 0`, `effGetPlugCategory`,
   `effGetProgramNameIndexed` (falling back to walking programs with
   `effSetProgram`/`effGetProgramName` when it returns 0),
   `effCanDo "receiveVstMidiEvent"`, read `initialDelay`, `effGetEffectName`.
2. **Prepare.** `effSetSpeakerArrangement` (stereo), `effSetSampleRate`,
   `effSetBlockSize`, `effMainsChanged(1)`, the *parameter 0 toggle quirk*
   (when the plugin has no editor the MPC sets parameter 0 to 1.0 or 0.0 and
   straight back; your parameter 0 must tolerate this), `effStartProcess`.
3. **Main render** of `--seconds` in blocks of `--block` frames. Instruments
   get note-ons for `--notes` at 0.1 s and note-offs at 60 % of the duration
   through `effProcessEvents` (which is called every block, even with no
   events, like the MPC does). Effects get a 220 Hz sine at -12 dBFS on every
   input plus a 50 ms burst of noise at 0.5 s. The MPC never sends MIDI to
   effects, so neither does host-sim.
4. **Block sizes 32 and 512.** The plugin is re-prepared the way the MPC
   does (`effMainsChanged(0)`, `effStopProcess`, `effSetBlockSize`,
   `effMainsChanged(1)`, toggle quirk, `effStartProcess`). Note the MPC
   *tells* you a block size of at least 128 but may *deliver* fewer frames:
   the 32-frame pass announces 128 and delivers 32.
5. **Random parameter sweep.** 200 random `setParameter` calls between
   blocks; each must round-trip through `getParameter` within 1e-4 (a plugin
   that quantises discrete parameters is accepted as long as the display text
   is unchanged) and the audio must stay finite.
6. **State round-trip.** `effGetChunk(0)`, randomise all parameters,
   `effSetChunk`, then every parameter must equal its saved value within 1e-4
   (same quantisation allowance). The re-saved chunk is compared to the
   original, and `effSetChunk` is fed garbage to make sure it does not crash.
7. **In-place processing** (effects only). The output is rendered once with
   separate input/output buffers and once with the *same* buffers for both,
   which is how JUCE-based hosts call `processReplacing`. If the in-place
   output nearly vanishes, you are clearing or writing outputs before reading
   the inputs.
8. **All-notes-off and 0.5 s of silence.** Instruments should go quiet
   (long releases produce a WARN, not a FAIL); effects should pass silence.
9. **Output checks** on every pass: NaN or Inf is a FAIL; `|x| > 1.0` is a
   WARN ("clipping"); denormals (`|x| < 1e-30` and not zero) are a WARN; an
   instrument that produces nothing for its notes is a WARN.
10. **Timing.** Wall time per `processReplacing` call versus the real-time
    budget of a block, reported as average and worst-case CPU %. Your desktop
    is far faster than the MPC's ARM CPU, so treat this as orientation only;
    an average above 50 % is a WARN.
11. **Teardown.** `effStopProcess`, `effMainsChanged(0)`, `effClose`.

The host answers the callbacks the MPC answers: `audioMasterVersion` = 2400,
vendor and product strings "MPC 3", vendor version 0x0101, sample rate, block
size, `audioMasterGetTime` (tempo 120, 4/4, positions advancing, playing only
with `--playing`), `audioMasterCanDo` for `sendVstEvents`, `sendVstMidiEvent`,
`sendVstTimeInfo`, `receiveVstEvents`, `receiveVstMidiEvent`, `sizeWindow`,
`supplyIdle`, plus `audioMasterAutomate`, `audioMasterUpdateDisplay`,
`audioMasterIOChanged` (re-reads `initialDelay`) and
`audioMasterProcessEvents` (MIDI out is counted and otherwise ignored, as on
the MPC). Everything else returns 0.

## Reading the report

* `FAIL` means the MPC would reject the plugin, crash, or produce broken
  audio. Fix these first.
* `WARN` is something that is probably a bug but may be intentional (a
  distortion that clips on purpose, a pad with a long release).
* `INFO` lines are facts about your plugin that are useful when debugging.

Run with `--verbose` to see every opcode. If the plugin crashes you get a line
like `CRASH SIGSEGV ... phase: render (main pass) ... last call:
processReplacing(128 frames)` and exit code 2.

## Limits

* It is a simulation of the MPC's *host behaviour*, not of its CPU. A plugin
  that passes here can still be too slow on the hardware; keep an eye on the
  timing line and test on the device.
* `dump` infers parameter kinds from display text, so a parameter whose text
  does not change with its value is reported as `float`.
* Windows support is written (`LoadLibrary`, `#ifdef _WIN32`) but untested.
