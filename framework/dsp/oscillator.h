// dsp/oscillator.h - anti-aliased (PolyBLEP) saw / square / triangle, plus sine.
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   mpc::dsp::PolyBlepOsc osc;
//   prepare():   osc.setSampleRate(sr);
//   note on:     osc.resetPhase();                      // optional: hard sync / click-free start
//   per block:   osc.setWave(mpc::dsp::Wave::Saw);      // Sine, Triangle, Saw, Square
//                osc.setFrequency(hz);                  // cheap: one divide
//                osc.setPulseWidth(0.5f);               // Square only, 0.05..0.95
//   per sample:  float y = osc.process();               // -1..1
//
//   mpc::dsp::WavetableSine sine;  sine.setSampleRate(sr); sine.setFrequency(hz); y = sine.process();
//     (table lookup with linear interpolation; cheaper than std::sin, use for drum bodies, FM, LFOs)
//
// Costs: Saw ~10 ops/sample, Square ~14, Triangle ~16, Sine ~6. No allocation.
// Wave can be used directly as the choice index of a choiceParam {"Sine","Triangle","Saw","Square"}.

#ifndef MPC_DSP_OSCILLATOR_H
#define MPC_DSP_OSCILLATOR_H

#include <cmath>
#include "math.h"

namespace mpc {
namespace dsp {

enum class Wave { Sine = 0, Triangle = 1, Saw = 2, Square = 3 };

class PolyBlepOsc
{
public:
    void setSampleRate(double sr) { sr_ = (float)sr; setFrequency(hz_); }
    void setWave(Wave w) { wave_ = w; }
    void setWave(int choiceIndex) { wave_ = (Wave)clamp(choiceIndex, 0, 3); }
    void setFrequency(float hz)
    {
        hz_ = hz;
        inc_ = hz / sr_;
        if (inc_ > 0.49f) inc_ = 0.49f;   // never above Nyquist
        if (inc_ < 0.0f) inc_ = 0.0f;
    }
    void setPulseWidth(float pw) { pw_ = clamp(pw, 0.05f, 0.95f); }
    void resetPhase(float phase01 = 0.0f)
    {
        phase_ = wrapPhase(phase01);
        tri_ = phase_ < 0.5f ? 4.0f * phase_ - 1.0f : 3.0f - 4.0f * phase_;   // start the integrator where a triangle would be
    }
    float phase() const { return phase_; }

    float process()
    {
        const float t = phase_;
        float y;
        switch (wave_)
        {
            case Wave::Sine:
                y = std::sin(kTwoPi * t);
                break;
            case Wave::Saw:
                y = 2.0f * t - 1.0f;
                y -= polyBlep(t, inc_);
                break;
            case Wave::Square:
                y = squareBlep(t);
                break;
            case Wave::Triangle:
            default:
            {
                // Integrate a band-limited square (leaky, so DC drifts away).
                const float sq = squareBlepSym(t);
                tri_ = tri_ * (1.0f - inc_ * 0.5f) + sq * 4.0f * inc_;
                // The leaky integrator slightly reduces the level at high frequencies; fine.
                y = tri_;
                break;
            }
        }
        phase_ += inc_;
        if (phase_ >= 1.0f) phase_ -= 1.0f;
        return y;
    }

private:
    // Standard 2-sample polynomial band-limited step correction.
    static float polyBlep(float t, float dt)
    {
        if (dt <= 0.0f) return 0.0f;
        if (t < dt) { t /= dt; return t + t - t * t - 1.0f; }
        if (t > 1.0f - dt) { t = (t - 1.0f) / dt; return t * t + t + t + 1.0f; }
        return 0.0f;
    }
    float squareBlep(float t) const
    {
        float y = t < pw_ ? 1.0f : -1.0f;
        y += polyBlep(t, inc_);
        y -= polyBlep(wrapPhase(t + 1.0f - pw_), inc_);
        return y;
    }
    float squareBlepSym(float t) const      // 50% duty version for the triangle
    {
        float y = t < 0.5f ? 1.0f : -1.0f;
        y += polyBlep(t, inc_);
        y -= polyBlep(wrapPhase(t + 0.5f), inc_);
        return y;
    }

    Wave wave_ = Wave::Saw;
    float sr_ = 44100.0f, hz_ = 440.0f, inc_ = 440.0f / 44100.0f;
    float phase_ = 0.0f, pw_ = 0.5f, tri_ = 0.0f;
};

// Sine via a 1024-entry table with linear interpolation. Shared table, built on first use
// (static local - built during construction/prepare of the first instance, not per sample).
class WavetableSine
{
public:
    WavetableSine() { table(); }   // force table construction outside the audio thread
    void setSampleRate(double sr) { sr_ = (float)sr; setFrequency(hz_); }
    void setFrequency(float hz) { hz_ = hz; inc_ = hz / sr_; }
    void resetPhase(float phase01 = 0.0f) { phase_ = wrapPhase(phase01); }
    float phase() const { return phase_; }
    // Phase modulation input in cycles (FM/PM synthesis): y = sin(2pi (phase + pm)).
    float process(float pm = 0.0f)
    {
        const float y = lookup(phase_ + pm);
        phase_ += inc_;
        phase_ -= (float)(int)phase_;   // works for inc in [0,1)
        if (phase_ < 0.0f) phase_ += 1.0f;
        return y;
    }
    static float lookup(float phase01)
    {
        const float* tab = table();
        const float p = wrapPhase(phase01) * (float)kSize;
        const int i = (int)p;
        const float f = p - (float)i;
        return tab[i] + f * (tab[i + 1] - tab[i]);
    }

private:
    static const int kSize = 1024;
    static const float* table()
    {
        static float tab[kSize + 1];
        static bool built = false;
        if (!built)
        {
            for (int i = 0; i <= kSize; ++i) tab[i] = std::sin(kTwoPi * (float)i / (float)kSize);
            built = true;
        }
        return tab;
    }
    float sr_ = 44100.0f, hz_ = 440.0f, inc_ = 440.0f / 44100.0f, phase_ = 0.0f;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_OSCILLATOR_H
