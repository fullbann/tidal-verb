// dsp/lfo.h - low frequency oscillator: sine / triangle / saw / square / sample & hold.
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   mpc::dsp::LFO lfo;
//   prepare():   lfo.setSampleRate(sr);
//   per block:   lfo.setShape(getInt(kShape));                  // matches shapeNames() order
//                if (getBool(kSync)) lfo.setTempoSync(ctx.transport.bpm, divisionBeats(getInt(kDiv)));
//                else                lfo.setFrequency(get(kRateHz));
//                if (getBool(kSync) && ctx.transport.playing) lfo.syncToPpq(ctx.transport.ppqPos);
//                      // optional: lock phase to the song position each block
//   per sample:  float m = lfo.process();          // bipolar -1..1
//                float u = lfo.unipolar();         // 0..1 version of the last output
//   note on:     lfo.reset(phase01);               // retrigger
// The sine shape uses a parabolic approximation (fine for modulation). No allocation.

#ifndef MPC_DSP_LFO_H
#define MPC_DSP_LFO_H

#include <cmath>
#include <string>
#include <vector>
#include "math.h"
#include "noise.h"
#include "tempo.h"

namespace mpc {
namespace dsp {

class LFO
{
public:
    enum class Shape { Sine = 0, Triangle, Saw, Square, SampleHold };
    static std::vector<std::string> shapeNames() { return {"Sine", "Triangle", "Saw", "Square", "S&H"}; }   // constructor only (allocates)

    void setSampleRate(double sr) { sr_ = (float)sr; setFrequency(hz_); }
    void setShape(Shape s) { shape_ = s; }
    void setShape(int choiceIndex) { shape_ = (Shape)clamp(choiceIndex, 0, 4); }
    void setFrequency(float hz) { hz_ = hz; inc_ = clamp(hz / sr_, 0.0f, 0.5f); }
    // One cycle per `beats` quarter notes at the given tempo.
    void setTempoSync(double bpm, float beats) { setFrequency(beatsToHz(beats, bpm)); beats_ = beats; }
    // Jump to the phase implied by the song position (call per block while playing).
    void syncToPpq(double ppqPos) { if (beats_ > 0.0f) phase_ = wrapPhase(ppqPhase(ppqPos, beats_) + offset_); }
    // Phase offset 0..1 added to the sync phase / used by reset().
    void setPhaseOffset(float phase01) { offset_ = wrapPhase(phase01); }
    void reset(float phase01 = 0.0f) { phase_ = wrapPhase(phase01 + offset_); held_ = rng_.nextBipolar(); last_ = 0.0f; }

    float process()
    {
        float y;
        switch (shape_)
        {
            case Shape::Sine:     y = fastSin01(phase_); break;
            case Shape::Triangle: y = phase_ < 0.5f ? 4.0f * phase_ - 1.0f : 3.0f - 4.0f * phase_; break;
            case Shape::Saw:      y = 1.0f - 2.0f * phase_; break;                 // falls from +1 to -1
            case Shape::Square:   y = phase_ < 0.5f ? 1.0f : -1.0f; break;
            case Shape::SampleHold:
            default:              y = held_; break;
        }
        phase_ += inc_;
        if (phase_ >= 1.0f) { phase_ -= 1.0f; held_ = rng_.nextBipolar(); }
        last_ = y;
        return y;
    }
    float last() const { return last_; }
    float unipolar() const { return 0.5f * (last_ + 1.0f); }
    float phase() const { return phase_; }

private:
    Shape shape_ = Shape::Sine;
    float sr_ = 44100.0f, hz_ = 1.0f, inc_ = 1.0f / 44100.0f, beats_ = 0.0f, offset_ = 0.0f;
    float phase_ = 0.0f, held_ = 0.0f, last_ = 0.0f;
    Xorshift32 rng_{0xC0FFEEu};
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_LFO_H
