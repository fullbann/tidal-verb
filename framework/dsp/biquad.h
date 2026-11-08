// dsp/biquad.h - RBJ "Audio EQ Cookbook" biquads (transposed direct form II).
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   using namespace mpc::dsp;
//   Biquad eq;
//   eq.setCoeffs(BiquadCoeffs::peak(sr, 1000.0f, 1.0f, +6.0f));    // per block when params change
//   y = eq.process(x);                                              // per sample
//
//   StereoBiquad lp;  lp.setCoeffs(BiquadCoeffs::lowpass(sr, 8000.0f, 0.707f));
//   lp.process(left, right);                                        // in place
//   lp.processBlock(outL, outR, numFrames);                         // in place, whole block
//
// Available: lowpass, highpass, bandpass (constant 0 dB peak), notch, allpass,
// peak (gainDb), lowShelf (gainDb), highShelf (gainDb). Q ~0.707 is "flat".
// Coefficient calculation uses sin/cos/pow: call it per block, not per sample.
// For audio-rate cutoff modulation use dsp/svf.h instead.

#ifndef MPC_DSP_BIQUAD_H
#define MPC_DSP_BIQUAD_H

#include <cmath>

namespace mpc {
namespace dsp {

struct BiquadCoeffs
{
    float b0 = 1.0f, b1 = 0.0f, b2 = 0.0f, a1 = 0.0f, a2 = 0.0f;   // normalised (a0 == 1)

    static BiquadCoeffs identity() { return BiquadCoeffs(); }

    static BiquadCoeffs lowpass(double sr, float hz, float q = 0.7071f)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        return normalise((1.0f - c) * 0.5f, 1.0f - c, (1.0f - c) * 0.5f, 1.0f + alpha, -2.0f * c, 1.0f - alpha);
    }
    static BiquadCoeffs highpass(double sr, float hz, float q = 0.7071f)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        return normalise((1.0f + c) * 0.5f, -(1.0f + c), (1.0f + c) * 0.5f, 1.0f + alpha, -2.0f * c, 1.0f - alpha);
    }
    static BiquadCoeffs bandpass(double sr, float hz, float q = 1.0f)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        return normalise(alpha, 0.0f, -alpha, 1.0f + alpha, -2.0f * c, 1.0f - alpha);
    }
    static BiquadCoeffs notch(double sr, float hz, float q = 1.0f)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        return normalise(1.0f, -2.0f * c, 1.0f, 1.0f + alpha, -2.0f * c, 1.0f - alpha);
    }
    static BiquadCoeffs allpass(double sr, float hz, float q = 0.7071f)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        return normalise(1.0f - alpha, -2.0f * c, 1.0f + alpha, 1.0f + alpha, -2.0f * c, 1.0f - alpha);
    }
    static BiquadCoeffs peak(double sr, float hz, float q, float gainDb)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        const float A = std::pow(10.0f, gainDb / 40.0f);
        return normalise(1.0f + alpha * A, -2.0f * c, 1.0f - alpha * A, 1.0f + alpha / A, -2.0f * c, 1.0f - alpha / A);
    }
    static BiquadCoeffs lowShelf(double sr, float hz, float q, float gainDb)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        const float A = std::pow(10.0f, gainDb / 40.0f);
        const float sa = 2.0f * std::sqrt(A) * alpha;
        return normalise(A * ((A + 1.0f) - (A - 1.0f) * c + sa),
                         2.0f * A * ((A - 1.0f) - (A + 1.0f) * c),
                         A * ((A + 1.0f) - (A - 1.0f) * c - sa),
                         (A + 1.0f) + (A - 1.0f) * c + sa,
                         -2.0f * ((A - 1.0f) + (A + 1.0f) * c),
                         (A + 1.0f) + (A - 1.0f) * c - sa);
    }
    static BiquadCoeffs highShelf(double sr, float hz, float q, float gainDb)
    {
        float w, c, s, alpha; common(sr, hz, q, w, c, s, alpha);
        const float A = std::pow(10.0f, gainDb / 40.0f);
        const float sa = 2.0f * std::sqrt(A) * alpha;
        return normalise(A * ((A + 1.0f) + (A - 1.0f) * c + sa),
                         -2.0f * A * ((A - 1.0f) + (A + 1.0f) * c),
                         A * ((A + 1.0f) + (A - 1.0f) * c - sa),
                         (A + 1.0f) - (A - 1.0f) * c + sa,
                         2.0f * ((A - 1.0f) - (A + 1.0f) * c),
                         (A + 1.0f) - (A - 1.0f) * c - sa);
    }

private:
    static void common(double sr, float hz, float q, float& w, float& c, float& s, float& alpha)
    {
        const float nyq = 0.49f * (float)sr;
        if (hz > nyq) hz = nyq;
        if (hz < 1.0f) hz = 1.0f;
        if (q < 0.025f) q = 0.025f;
        w = 2.0f * 3.14159265358979f * hz / (float)sr;
        c = std::cos(w); s = std::sin(w);
        alpha = s / (2.0f * q);
    }
    static BiquadCoeffs normalise(float b0, float b1, float b2, float a0, float a1, float a2)
    {
        BiquadCoeffs k; const float inv = 1.0f / a0;
        k.b0 = b0 * inv; k.b1 = b1 * inv; k.b2 = b2 * inv; k.a1 = a1 * inv; k.a2 = a2 * inv;
        return k;
    }
};

class Biquad
{
public:
    void setCoeffs(const BiquadCoeffs& c) { c_ = c; }
    const BiquadCoeffs& coeffs() const { return c_; }
    void reset() { z1_ = z2_ = 0.0f; }
    float process(float x)
    {
        const float y = c_.b0 * x + z1_;
        z1_ = c_.b1 * x - c_.a1 * y + z2_;
        z2_ = c_.b2 * x - c_.a2 * y;
        return y;
    }
    void processBlock(float* inout, int n) { for (int i = 0; i < n; ++i) inout[i] = process(inout[i]); }
private:
    BiquadCoeffs c_;
    float z1_ = 0.0f, z2_ = 0.0f;
};

class StereoBiquad
{
public:
    void setCoeffs(const BiquadCoeffs& c) { l_.setCoeffs(c); r_.setCoeffs(c); }
    void reset() { l_.reset(); r_.reset(); }
    void process(float& left, float& right) { left = l_.process(left); right = r_.process(right); }
    void processBlock(float* left, float* right, int n) { l_.processBlock(left, n); r_.processBlock(right, n); }
    Biquad& left() { return l_; }
    Biquad& right() { return r_; }
private:
    Biquad l_, r_;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_BIQUAD_H
