// dsp/onepole.h - the cheapest useful filters: 6 dB/oct low/high pass and a DC blocker.
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   mpc::dsp::OnePoleLP lp;   lp.setCutoff(2000.0f, sr);   y = lp.process(x);
//   mpc::dsp::OnePoleHP hp;   hp.setCutoff(80.0f, sr);     y = hp.process(x);
//   mpc::dsp::DCBlocker dc;   dc.prepare(sr);              y = dc.process(x);   // removes DC offset
// setCutoff() costs one exp(); call it per block, not per sample, unless modulating.
// All classes: reset() clears state; no allocation.

#ifndef MPC_DSP_ONEPOLE_H
#define MPC_DSP_ONEPOLE_H

#include <cmath>

namespace mpc {
namespace dsp {

class OnePoleLP
{
public:
    void setCutoff(float hz, double sampleRate)
    {
        const float x = 2.0f * 3.14159265f * hz / (float)sampleRate;
        a_ = 1.0f - std::exp(-(x < 0.0f ? 0.0f : x));
        if (a_ > 1.0f) a_ = 1.0f;
    }
    // Directly set the coefficient (0..1, 1 = no filtering).
    void setCoeff(float a) { a_ = a; }
    void reset(float value = 0.0f) { z_ = value; }
    float process(float x) { z_ += a_ * (x - z_); return z_; }
    float last() const { return z_; }
private:
    float a_ = 1.0f, z_ = 0.0f;
};

class OnePoleHP
{
public:
    void setCutoff(float hz, double sampleRate) { lp_.setCutoff(hz, sampleRate); }
    void reset() { lp_.reset(); }
    float process(float x) { return x - lp_.process(x); }
private:
    OnePoleLP lp_;
};

// y[n] = x[n] - x[n-1] + R * y[n-1]; R chosen for ~cutoffHz.
class DCBlocker
{
public:
    void prepare(double sampleRate, float cutoffHz = 10.0f)
    {
        r_ = 1.0f - (2.0f * 3.14159265f * cutoffHz / (float)sampleRate);
        if (r_ < 0.0f) r_ = 0.0f;
    }
    void reset() { x1_ = y1_ = 0.0f; }
    float process(float x)
    {
        const float y = x - x1_ + r_ * y1_;
        x1_ = x; y1_ = y;
        return y;
    }
private:
    float r_ = 0.9986f, x1_ = 0.0f, y1_ = 0.0f;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_ONEPOLE_H
