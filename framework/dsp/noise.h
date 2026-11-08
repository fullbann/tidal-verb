// dsp/noise.h - white / pink noise and a tiny xorshift random generator.
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   mpc::dsp::Xorshift32 rng(1234);          // seed per voice so voices differ
//   float r = rng.nextFloat();                // [0, 1)
//   float b = rng.nextBipolar();              // [-1, 1)
//   mpc::dsp::WhiteNoise white;  float n = white.process();   // [-1, 1)
//   mpc::dsp::PinkNoise  pink;   float p = pink.process();    // ~[-1, 1] (RMS ~0.24), -3 dB/oct
// No allocation; everything is a few integer ops per sample.

#ifndef MPC_DSP_NOISE_H
#define MPC_DSP_NOISE_H

#include <cstdint>

namespace mpc {
namespace dsp {

class Xorshift32
{
public:
    explicit Xorshift32(uint32_t seed = 0x9E3779B9u) { reseed(seed); }
    void reseed(uint32_t seed) { state_ = seed != 0 ? seed : 0x9E3779B9u; }
    uint32_t next()
    {
        uint32_t x = state_;
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        state_ = x;
        return x;
    }
    float nextFloat()   { return (float)(next() >> 8) * (1.0f / 16777216.0f); }        // [0,1)
    float nextBipolar() { return (float)(next() >> 8) * (2.0f / 16777216.0f) - 1.0f; } // [-1,1)
private:
    uint32_t state_;
};

class WhiteNoise
{
public:
    explicit WhiteNoise(uint32_t seed = 0x1234567u) : rng_(seed) {}
    void reseed(uint32_t seed) { rng_.reseed(seed); }
    float process() { return rng_.nextBipolar(); }
private:
    Xorshift32 rng_;
};

// Paul Kellet's economy pink noise filter (good enough for audio).
class PinkNoise
{
public:
    explicit PinkNoise(uint32_t seed = 0x7654321u) : rng_(seed) {}
    void reseed(uint32_t seed) { rng_.reseed(seed); }
    void reset() { b0_ = b1_ = b2_ = 0.0f; }
    float process()
    {
        const float white = rng_.nextBipolar();
        b0_ = 0.99765f * b0_ + white * 0.0990460f;
        b1_ = 0.96300f * b1_ + white * 0.2965164f;
        b2_ = 0.57000f * b2_ + white * 1.0526913f;
        return (b0_ + b1_ + b2_ + white * 0.1848f) * 0.14f;   // peaks ~1.0, RMS ~0.24
    }
private:
    Xorshift32 rng_;
    float b0_ = 0.0f, b1_ = 0.0f, b2_ = 0.0f;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_NOISE_H
