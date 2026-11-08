// dsp/voice.h - VoiceAllocator<N>: note on/off bookkeeping for polyphonic synths.
//
// SPDX-License-Identifier: MIT
//
// It does NOT make sound; it tells you which of your N voice objects to start or
// release, handles voice stealing (oldest released voice first, then the oldest
// held one), sustain pedal and pitch bend.
//
// Usage inside process(), for each MidiEvent e:
//   mpc::dsp::VoiceAllocator<8> alloc;
//   if (e.isNoteOn())  { int v = alloc.noteOn(e.note(), e.velocity()); voices[v].start(e.note(), e.velocity()); }
//   if (e.isNoteOff()) { int v = alloc.noteOff(e.note()); if (v >= 0) voices[v].release(); }
//                      // returns -1 when the sustain pedal is down (the note is kept until pedal up)
//   if (e.isController() && e.controller() == 64)
//       alloc.setSustain(e.controllerValue() >= 64, [&](int v) { voices[v].release(); });
//   if (e.isPitchBend()) alloc.setPitchBend(e.pitchBend());       // -8192..8191
//   if (e.isAllNotesOff()) alloc.allNotesOff([&](int v) { voices[v].release(); });
// Per block/voice:
//   float hz = alloc.noteToHz(voices[v].note);   // includes pitch bend (bendRange semitones)
// When your voice's envelope finishes: alloc.voiceFinished(v);  so the slot is free again.
// Mono/legato synths: use N == 1; noteOn() then always returns 0 and lastNote() gives the newest key.

#ifndef MPC_DSP_VOICE_H
#define MPC_DSP_VOICE_H

#include <cmath>
#include <cstdint>

namespace mpc {
namespace dsp {

template <int N>
class VoiceAllocator
{
public:
    static constexpr int kNumVoices = N;

    struct Slot
    {
        int  note = -1;
        int  velocity = 0;
        bool active = false;      // producing sound (held, sustained or releasing)
        bool held = false;        // key is physically down
        bool sustained = false;   // key up, but the pedal keeps it going
        uint32_t order = 0;       // allocation counter for "oldest" decisions
    };

    void reset()
    {
        for (int i = 0; i < N; ++i) slots_[i] = Slot();
        counter_ = 0; sustain_ = false; lastNote_ = -1;
    }

    // Returns the voice index to (re)start. *stolen is set when that voice was still sounding.
    int noteOn(int note, int velocity, bool* stolen = nullptr)
    {
        int v = findFree();
        bool steal = false;
        if (v < 0) { v = findOldest(false); if (v < 0) v = findOldest(true); steal = true; }
        if (v < 0) v = 0;
        Slot& s = slots_[v];
        s.note = note; s.velocity = velocity; s.active = true; s.held = true; s.sustained = false; s.order = ++counter_;
        lastNote_ = note;
        if (stolen) *stolen = steal;
        return v;
    }

    // Returns the voice index to release, or -1 (unknown note, or pedal down: it is marked sustained).
    int noteOff(int note)
    {
        int best = -1;
        for (int i = 0; i < N; ++i)
            if (slots_[i].active && slots_[i].held && slots_[i].note == note)
                if (best < 0 || slots_[i].order < slots_[best].order) best = i;
        if (best < 0) return -1;
        slots_[best].held = false;
        if (sustain_) { slots_[best].sustained = true; return -1; }
        return best;
    }

    // Sustain pedal (CC 64). onRelease(v) is called for every voice that must now release.
    template <typename F>
    void setSustain(bool down, F onRelease)
    {
        sustain_ = down;
        if (down) return;
        for (int i = 0; i < N; ++i)
            if (slots_[i].active && slots_[i].sustained) { slots_[i].sustained = false; onRelease(i); }
    }
    bool sustain() const { return sustain_; }

    template <typename F>
    void allNotesOff(F onRelease)
    {
        for (int i = 0; i < N; ++i)
            if (slots_[i].active && (slots_[i].held || slots_[i].sustained)) { slots_[i].held = false; slots_[i].sustained = false; onRelease(i); }
    }

    void voiceFinished(int v) { if (v >= 0 && v < N) slots_[v] = Slot(); }

    // Pitch bend: raw -8192..8191 (MidiEvent::pitchBend()) with range in semitones.
    void setBendRange(float semitones) { bendRange_ = semitones; updateBend(); }
    void setPitchBend(int raw) { bend_ = (float)raw / 8192.0f; updateBend(); }
    void setPitchBendNormalised(float bipolar) { bend_ = bipolar; updateBend(); }
    float bendRatio() const { return bendRatio_; }
    float bendSemitones() const { return bend_ * bendRange_; }
    float noteToHz(float note, float a4 = 440.0f) const { return a4 * std::pow(2.0f, (note - 69.0f) / 12.0f) * bendRatio_; }

    const Slot& slot(int v) const { return slots_[v]; }
    bool isActive(int v) const { return slots_[v].active; }
    bool isHeld(int v) const { return slots_[v].held; }
    int lastNote() const { return lastNote_; }
    int numActive() const { int n = 0; for (int i = 0; i < N; ++i) n += slots_[i].active ? 1 : 0; return n; }
    // Highest held note, or -1 (useful for mono synths with note priority).
    int highestHeld() const { int h = -1; for (int i = 0; i < N; ++i) if (slots_[i].active && slots_[i].held && slots_[i].note > h) h = slots_[i].note; return h; }

private:
    int findFree() const { for (int i = 0; i < N; ++i) if (!slots_[i].active) return i; return -1; }
    // includeHeld = false: only voices in release / sustained; true: any
    int findOldest(bool includeHeld) const
    {
        int best = -1;
        for (int i = 0; i < N; ++i)
        {
            if (!slots_[i].active) continue;
            if (!includeHeld && slots_[i].held) continue;
            if (best < 0 || slots_[i].order < slots_[best].order) best = i;
        }
        return best;
    }
    void updateBend() { bendRatio_ = std::pow(2.0f, bend_ * bendRange_ / 12.0f); }

    Slot slots_[N];
    uint32_t counter_ = 0;
    bool sustain_ = false;
    int lastNote_ = -1;
    float bend_ = 0.0f, bendRange_ = 2.0f, bendRatio_ = 1.0f;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_VOICE_H
