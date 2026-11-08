// dsp/svf.h - Cytomic / Andy Simper topology-preserving state variable filter.
//
// SPDX-License-Identifier: MIT
//
// Stable under fast cutoff modulation (envelopes, LFOs), 12 dB/oct, self-oscillates
// near resonance 1.0. This is the filter to use inside synth voices.
//
// Usage:
//   mpc::dsp::SVF filt;
//   prepare(): filt.setSampleRate(sr);
//   per block (or every N samples when modulating):
//              filt.set(cutoffHz, resonance01);      // one tan() per call
//              filt.setDrive(1.0f .. 10.0f);          // optional input saturation
//   per sample:
//              y = filt.process(x, mpc::dsp::SVF::Mode::Lowpass);
//      or      filt.processAll(x, lp, bp, hp);        // all outputs at once
//   filt.reset() in reset().
// For stereo use two instances. No allocation.

#ifndef MPC_DSP_SVF_H
#define MPC_DSP_SVF_H

#include <cmath>
#include "math.h"

namespace mpc {
namespace dsp {

class SVF
{
public:
    enum class Mode { Lowpass = 0, Bandpass, Highpass, Notch, Peak };

    void setSampleRate(double sr) { sr_ = (float)sr; nyq_ = 0.49f * sr_; }

    // cutoff in Hz, resonance 0..1 (0 = flat ~Q 0.5, 1 = self-oscillation).
    void set(float cutoffHz, float resonance)
    {
        cutoffHz = clamp(cutoffHz, 5.0f, nyq_);
        resonance = clamp(resonance, 0.0f, 1.0f);
        g_ = std::tan(kPi * cutoffHz / sr_);
        k_ = 2.0f - 1.98f * resonance;              // k = 1/Q; 2 -> Q 0.5, 0.02 -> near self-osc
        a1_ = 1.0f / (1.0f + g_ * (g_ + k_));
        a2_ = g_ * a1_;
        a3_ = g_ * a2_;
    }
    // drive >= 1. Values above 1 saturate the input with a tanh curve.
    void setDrive(float drive) { drive_ = drive < 1.0f ? 1.0f : drive; }

    void reset() { ic1eq_ = ic2eq_ = 0.0f; }

    // Compute lp/bp/hp in one go (notch = x - k*bp, peak = lp - hp).
    void processAll(float x, float& lp, float& bp, float& hp)
    {
        if (drive_ > 1.0f) x = fastTanh(x * drive_) / fastTanh(drive_);
        const float v3 = x - ic2eq_;
        const float v1 = a1_ * ic1eq_ + a2_ * v3;
        const float v2 = ic2eq_ + a2_ * ic1eq_ + a3_ * v3;
        ic1eq_ = 2.0f * v1 - ic1eq_;
        ic2eq_ = 2.0f * v2 - ic2eq_;
        lp = v2; bp = v1; hp = x - k_ * v1 - v2;
    }

    float process(float x, Mode mode)
    {
        float lp, bp, hp;
        processAll(x, lp, bp, hp);
        switch (mode)
        {
            case Mode::Lowpass:  return lp;
            case Mode::Bandpass: return bp;
            case Mode::Highpass: return hp;
            case Mode::Notch:    return lp + hp;
            case Mode::Peak:     return lp - hp;
        }
        return lp;
    }

    // Convenience: mode as an int matching a choiceParam {"LP","BP","HP","Notch"}.
    float process(float x, int modeIndex) { return process(x, (Mode)clamp(modeIndex, 0, 4)); }

private:
    float sr_ = 44100.0f, nyq_ = 21609.0f;
    float g_ = 0.1f, k_ = 1.0f, a1_ = 1.0f, a2_ = 0.0f, a3_ = 0.0f;
    float ic1eq_ = 0.0f, ic2eq_ = 0.0f;
    float drive_ = 1.0f;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_SVF_H
