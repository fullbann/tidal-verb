// dsp/delay_line.h - a delay line with fractional (linear or allpass) read.
//
// SPDX-License-Identifier: MIT
//
// THE ONLY ALLOCATION happens in prepare(maxDelaySamples). Call it from your
// Plugin::prepare(), never from process().
//
// Usage:
//   mpc::dsp::DelayLine dl;
//   prepare():   dl.prepare((int)(sr * 2.0) + 1);      // up to 2 s
//   reset():     dl.reset();
//   per sample:  float wet = dl.readLinear(delaySamples);   // delaySamples >= 1, may be fractional
//                dl.write(in + wet * feedback);
//     or         float wet = dl.process(in + fb, delaySamples);   // write then read (delay >= 1)
//   readAllpass(d): flatter frequency response for modulated/chorus use (keeps its own state;
//                   call it exactly once per sample).
//   readInt(n):     exact integer tap, cheapest.
// Sizes round up to a power of two so the wrap is a mask, not a branch.

#ifndef MPC_DSP_DELAY_LINE_H
#define MPC_DSP_DELAY_LINE_H

#include <vector>
#include <cstring>
#include <cmath>

namespace mpc {
namespace dsp {

class DelayLine
{
public:
    // Allocates. Prepare-time only.
    void prepare(int maxDelaySamples)
    {
        int size = 16;
        while (size < maxDelaySamples + 2) size <<= 1;
        buffer_.assign((size_t)size, 0.0f);
        mask_ = size - 1;
        maxDelay_ = maxDelaySamples;
        write_ = 0;
        ap_ = 0.0f;
    }
    void setMaxDelay(int maxDelaySamples) { prepare(maxDelaySamples); }   // alias; also prepare-time only
    int maxDelay() const { return maxDelay_; }
    bool isPrepared() const { return !buffer_.empty(); }

    void reset()
    {
        if (!buffer_.empty()) std::memset(buffer_.data(), 0, sizeof(float) * buffer_.size());
        write_ = 0; ap_ = 0.0f;
    }

    void write(float x)
    {
        buffer_[(size_t)write_] = x;
        write_ = (write_ + 1) & mask_;
    }

    float readInt(int delaySamples) const
    {
        delaySamples = clampDelay(delaySamples);
        return buffer_[(size_t)((write_ - delaySamples) & mask_)];
    }

    float readLinear(float delaySamples) const
    {
        if (delaySamples < 1.0f) delaySamples = 1.0f;
        if (delaySamples > (float)maxDelay_) delaySamples = (float)maxDelay_;
        const int   d  = (int)delaySamples;
        const float f  = delaySamples - (float)d;
        const int   i0 = (write_ - d) & mask_;
        const int   i1 = (i0 - 1) & mask_;
        const float a = buffer_[(size_t)i0], b = buffer_[(size_t)i1];
        return a + f * (b - a);
    }

    // First-order allpass interpolation (Dattorro). Call once per sample.
    float readAllpass(float delaySamples)
    {
        if (delaySamples < 1.0f) delaySamples = 1.0f;
        if (delaySamples > (float)maxDelay_ - 1.0f) delaySamples = (float)maxDelay_ - 1.0f;
        int   d = (int)delaySamples;
        float f = delaySamples - (float)d;
        if (f < 0.1f) { f += 1.0f; --d; if (d < 1) { d = 1; f = 0.1f; } }   // keep the allpass coefficient stable
        const float eta = (1.0f - f) / (1.0f + f);
        const int   i0 = (write_ - d) & mask_;
        const int   i1 = (i0 - 1) & mask_;
        const float y = buffer_[(size_t)i1] + eta * (buffer_[(size_t)i0] - ap_);
        ap_ = y;
        return y;
    }

    // write() then readLinear(): a plain feed-forward delay.
    float process(float x, float delaySamples) { write(x); return readLinear(delaySamples); }

private:
    int clampDelay(int d) const { return d < 1 ? 1 : (d > maxDelay_ ? maxDelay_ : d); }

    std::vector<float> buffer_;
    int mask_ = 0, maxDelay_ = 0, write_ = 0;
    float ap_ = 0.0f;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_DELAY_LINE_H
