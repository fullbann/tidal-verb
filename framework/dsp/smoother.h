// dsp/smoother.h - parameter smoothing so knob moves never click.
//
// SPDX-License-Identifier: MIT
//
// Two tools:
//
//   Smoother (one-pole, per sample). Good for cutoff, gain, delay time.
//     mpc::dsp::Smoother gain;
//     prepare():  gain.setTime(20.0f, sampleRate); gain.snap(get(kGain));
//     process():  gain.setTarget(get(kGain));
//                 for each sample: out[i] = in[i] * gain.process();
//
//   LinearRamp (per block). Cheaper: one divide per block, one add per sample.
//     mpc::dsp::LinearRamp vol;
//     prepare():  vol.snap(get(kVol));
//     process():  vol.setTarget(get(kVol), ctx.numFrames);
//                 for each sample: out[i] = in[i] * vol.next();
//     Reaches the target exactly at the end of the block, so a 128-frame block
//     gives a ~3 ms ramp. Use Smoother if you want a fixed time constant.
//
// Both are allocation free and safe to call from process().

#ifndef MPC_DSP_SMOOTHER_H
#define MPC_DSP_SMOOTHER_H

#include <cmath>

namespace mpc {
namespace dsp {

class Smoother
{
public:
    // Time to reach ~63% of a step. Call from prepare() (or when the time changes).
    void setTime(float ms, double sampleRate)
    {
        const float n = ms * 0.001f * (float)sampleRate;
        coeff_ = n <= 1.0f ? 1.0f : 1.0f - std::exp(-1.0f / n);
    }
    void setTarget(float target) { target_ = target; }
    // Jump immediately (use in prepare/reset and when a parameter should not glide).
    void snap(float value) { value_ = target_ = value; }
    float process()
    {
        value_ += coeff_ * (target_ - value_);
        // Snap when close so isSmoothing() goes false and no denormals linger.
        if (std::fabs(target_ - value_) < 1.0e-6f) value_ = target_;
        return value_;
    }
    float value() const { return value_; }
    float target() const { return target_; }
    bool isSmoothing() const { return value_ != target_; }

private:
    float value_ = 0.0f, target_ = 0.0f, coeff_ = 1.0f;
};

class LinearRamp
{
public:
    void snap(float value) { value_ = target_ = value; step_ = 0.0f; remaining_ = 0; }
    // Ramp from the current value to `target` over `numFrames` calls of next().
    void setTarget(float target, int numFrames)
    {
        target_ = target;
        if (numFrames <= 0 || target == value_) { value_ = target; step_ = 0.0f; remaining_ = 0; return; }
        step_ = (target - value_) / (float)numFrames;
        remaining_ = numFrames;
    }
    float next()
    {
        if (remaining_ > 0) { value_ += step_; if (--remaining_ == 0) value_ = target_; }
        return value_;
    }
    float value() const { return value_; }
    float target() const { return target_; }
    bool isRamping() const { return remaining_ > 0; }

private:
    float value_ = 0.0f, target_ = 0.0f, step_ = 0.0f;
    int remaining_ = 0;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_SMOOTHER_H
