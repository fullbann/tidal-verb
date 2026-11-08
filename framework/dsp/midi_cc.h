// dsp/midi_cc.h - names for common MIDI controllers and a smoothed mod wheel / pitch bend tracker.
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   #include "dsp/midi_cc.h"
//   mpc::dsp::MidiControllers ctl;            // one per plugin
//   prepare():    ctl.prepare(sr, 10.0f);     // 10 ms smoothing
//   process():    for each MidiEvent e: if (ctl.handle(e)) continue;   // eats CC/bend/aftertouch
//                 per sample: float wheel = ctl.modWheel();      // 0..1 smoothed
//                             float bend  = ctl.pitchBend();     // -1..1 smoothed
//                             float at    = ctl.aftertouch();    // 0..1 smoothed
//                 (call ctl.tick() once per sample first to advance the smoothers)
//                 bool pedal = ctl.sustain();                    // CC 64 >= 64
//   Raw values: ctl.cc(74) returns the last 0..127 value of any controller.
//   Names: mpc::dsp::cc::ModWheel == 1, cc::Sustain == 64, mpc::dsp::ccName(74) == "Brightness".
// handle() is a template so this header does not depend on mpc_plugin.h; pass mpc::MidiEvent.

#ifndef MPC_DSP_MIDI_CC_H
#define MPC_DSP_MIDI_CC_H

#include <cstdint>
#include "smoother.h"

namespace mpc {
namespace dsp {

namespace cc {
enum : int {
    BankSelect = 0, ModWheel = 1, Breath = 2, Foot = 4, PortamentoTime = 5, DataEntry = 6, Volume = 7,
    Balance = 8, Pan = 10, Expression = 11, Effect1 = 12, Effect2 = 13,
    General1 = 16, General2 = 17, General3 = 18, General4 = 19,
    Sustain = 64, PortamentoSwitch = 65, Sostenuto = 66, SoftPedal = 67, Legato = 68, Hold2 = 69,
    SoundVariation = 70, Resonance = 71, ReleaseTime = 72, AttackTime = 73, Brightness = 74,
    DecayTime = 75, VibratoRate = 76, VibratoDepth = 77, VibratoDelay = 78,
    ReverbSend = 91, ChorusSend = 93,
    AllSoundOff = 120, ResetAllControllers = 121, LocalControl = 122, AllNotesOff = 123,
    OmniOff = 124, OmniOn = 125, MonoOn = 126, PolyOn = 127
};
} // namespace cc

inline const char* ccName(int number)
{
    switch (number)
    {
        case cc::ModWheel: return "Mod Wheel";     case cc::Breath: return "Breath";
        case cc::Foot: return "Foot";              case cc::PortamentoTime: return "Portamento";
        case cc::Volume: return "Volume";          case cc::Pan: return "Pan";
        case cc::Expression: return "Expression";  case cc::Sustain: return "Sustain";
        case cc::Sostenuto: return "Sostenuto";    case cc::SoftPedal: return "Soft Pedal";
        case cc::Resonance: return "Resonance";    case cc::ReleaseTime: return "Release";
        case cc::AttackTime: return "Attack";      case cc::Brightness: return "Brightness";
        case cc::DecayTime: return "Decay";        case cc::VibratoRate: return "Vib Rate";
        case cc::VibratoDepth: return "Vib Depth"; case cc::ReverbSend: return "Reverb";
        case cc::ChorusSend: return "Chorus";      case cc::AllSoundOff: return "All Sound Off";
        case cc::ResetAllControllers: return "Reset Ctrl"; case cc::AllNotesOff: return "All Notes Off";
        default: return "CC";
    }
}

class MidiControllers
{
public:
    void prepare(double sampleRate, float smoothMs = 10.0f)
    {
        wheel_.setTime(smoothMs, sampleRate); bend_.setTime(smoothMs, sampleRate); at_.setTime(smoothMs, sampleRate);
        reset();
    }
    // Keeps the raw CC table; snaps the smoothers to their current targets.
    void reset()
    {
        wheel_.snap(wheel_.target()); bend_.snap(bend_.target()); at_.snap(at_.target());
    }
    // Forget everything (e.g. on "reset all controllers").
    void clear()
    {
        for (int i = 0; i < 128; ++i) values_[i] = 0;
        values_[cc::Volume] = 100; values_[cc::Expression] = 127;
        wheel_.snap(0.0f); bend_.snap(0.0f); at_.snap(0.0f); sustain_ = false;
    }

    // Returns true if the event was a controller / pitch bend / aftertouch message.
    template <typename Event>
    bool handle(const Event& e)
    {
        if (e.isController())
        {
            const int n = e.controller(), v = e.controllerValue();
            if (n >= 0 && n < 128) values_[n] = (uint8_t)v;
            if (n == cc::ModWheel) wheel_.setTarget((float)v / 127.0f);
            else if (n == cc::Sustain) sustain_ = v >= 64;
            else if (n == cc::ResetAllControllers) { clear(); }
            return true;
        }
        if (e.isPitchBend()) { bend_.setTarget((float)e.pitchBend() / 8192.0f); return true; }
        if (e.isAftertouch()) { at_.setTarget((float)e.data[1] / 127.0f); return true; }
        return false;
    }

    // Advance the smoothers: once per sample (or once per block for control-rate use).
    void tick() { wheel_.process(); bend_.process(); at_.process(); }

    float modWheel() const { return wheel_.value(); }     // 0..1
    float pitchBend() const { return bend_.value(); }     // -1..1
    float aftertouch() const { return at_.value(); }      // 0..1
    bool  sustain() const { return sustain_; }
    int   cc(int number) const { return (number >= 0 && number < 128) ? values_[number] : 0; }
    float cc01(int number) const { return (float)cc(number) / 127.0f; }

private:
    Smoother wheel_, bend_, at_;
    uint8_t values_[128] = {0};
    bool sustain_ = false;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_MIDI_CC_H
