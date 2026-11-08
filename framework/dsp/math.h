// dsp/math.h - small numeric helpers shared by the other dsp headers.
//
// SPDX-License-Identifier: MIT
//
// Usage (for an AI coding agent writing MPC plugins):
//   #include "dsp/math.h"
//   using namespace mpc::dsp;
//   float g = dbToGain(-6.0f);            // 0.501
//   float y = fastTanh(x * drive);        // cheap saturation, |y| < 1
//   float v = lerp(a, b, 0.25f);          // a + (b - a) * t
//   float c = clamp(v, 0.0f, 1.0f);
//   float s = msToSamples(10.0f, sr);     // 441 at 44.1 kHz
//   x = flushDenormal(x);                 // keep feedback paths cheap (the framework also
//                                         // sets FTZ around process(); this is belt-and-braces)
// Everything here is constexpr/inline, allocation free and safe on the audio thread.

#ifndef MPC_DSP_MATH_H
#define MPC_DSP_MATH_H

#include <cmath>
#include <cstdint>
#include <cstring>

namespace mpc {
namespace dsp {

constexpr float kPi      = 3.14159265358979323846f;
constexpr float kTwoPi   = 6.28318530717958647692f;
constexpr float kHalfPi  = 1.57079632679489661923f;
constexpr float kSqrt2   = 1.41421356237309504880f;
constexpr float kInvSqrt2 = 0.70710678118654752440f;

// ---------------------------------------------------------------------------
// Ranges
// ---------------------------------------------------------------------------
template <typename T> constexpr T clamp(T v, T lo, T hi) { return v < lo ? lo : (v > hi ? hi : v); }
template <typename T> constexpr T lerp(T a, T b, T t) { return a + (b - a) * t; }
template <typename T> constexpr T sign(T v) { return v > T(0) ? T(1) : (v < T(0) ? T(-1) : T(0)); }
template <typename T> constexpr T sq(T v) { return v * v; }

// Map t in [0,1] to [lo,hi] and back.
inline float mapRange(float t, float lo, float hi) { return lo + (hi - lo) * t; }
inline float unmapRange(float v, float lo, float hi) { return hi == lo ? 0.0f : (v - lo) / (hi - lo); }

// Wrap a phase to [0,1). Works for any magnitude but is fastest for small overshoots.
inline float wrapPhase(float p)
{
    p -= (float)std::floor(p);
    return p;
}

// ---------------------------------------------------------------------------
// Decibels, gain, pitch
// ---------------------------------------------------------------------------
inline float dbToGain(float db) { return std::pow(10.0f, db * 0.05f); }
inline float gainToDb(float gain, float floorDb = -120.0f)
{
    return gain > 1.0e-6f ? 20.0f * std::log10(gain) : floorDb;
}
// 1 semitone = 2^(1/12).
inline float semitonesToRatio(float semitones) { return std::pow(2.0f, semitones * (1.0f / 12.0f)); }
inline float centsToRatio(float cents) { return std::pow(2.0f, cents * (1.0f / 1200.0f)); }
inline float hzToMidi(float hz, float a4 = 440.0f) { return 69.0f + 12.0f * std::log2(hz / a4); }
inline float midiToHz(float note, float a4 = 440.0f) { return a4 * std::pow(2.0f, (note - 69.0f) * (1.0f / 12.0f)); }

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------
inline float msToSamples(float ms, double sampleRate) { return ms * 0.001f * (float)sampleRate; }
inline float samplesToMs(float samples, double sampleRate) { return samples * 1000.0f / (float)sampleRate; }

// One-pole coefficient that reaches ~63% of a step in `ms`. y += c * (target - y).
inline float onePoleCoeff(float ms, double sampleRate)
{
    if (ms <= 0.0f) return 1.0f;
    return 1.0f - std::exp(-1.0f / (ms * 0.001f * (float)sampleRate));
}

// ---------------------------------------------------------------------------
// Saturation
// ---------------------------------------------------------------------------
// Padé-style tanh approximation; max error ~1e-3 in [-3,3], clamps to +-1 beyond.
inline float fastTanh(float x)
{
    if (x > 4.97f) return 1.0f;
    if (x < -4.97f) return -1.0f;
    const float x2 = x * x;
    const float a = x * (135135.0f + x2 * (17325.0f + x2 * (378.0f + x2)));
    const float b = 135135.0f + x2 * (62370.0f + x2 * (3150.0f + x2 * 28.0f));
    return a / b;
}
// Cubic soft clip: linear-ish near 0, saturates at +-1 for |x| >= 1.5.
inline float softClip(float x)
{
    if (x >= 1.5f) return 1.0f;
    if (x <= -1.5f) return -1.0f;
    return x - (4.0f / 27.0f) * x * x * x;
}
inline float hardClip(float x, float limit = 1.0f) { return clamp(x, -limit, limit); }

// ---------------------------------------------------------------------------
// Denormals / NaN safety
// ---------------------------------------------------------------------------
// Add to filter/delay feedback paths if you see CPU spikes on long tails.
inline float flushDenormal(float x) { return std::fabs(x) < 1.0e-15f ? 0.0f : x; }
// Replace NaN/inf with 0. Use at the output of unstable algorithms while developing.
inline float sanitize(float x) { return std::isfinite(x) ? x : 0.0f; }
inline bool isBad(float x) { return !std::isfinite(x); }

// ---------------------------------------------------------------------------
// Panning: equal-power. pan in [-1, 1].
// ---------------------------------------------------------------------------
inline void equalPowerPan(float pan, float& gainL, float& gainR)
{
    const float t = (clamp(pan, -1.0f, 1.0f) + 1.0f) * 0.25f * kPi; // 0..pi/2
    gainL = std::cos(t);
    gainR = std::sin(t);
}

// Fast sine for phase in [0,1) (parabolic approximation, ~1% THD; fine for LFOs).
inline float fastSin01(float phase)
{
    float x = 1.0f - 2.0f * wrapPhase(phase);   // 1..-1 so that phase 0.25 -> +1
    return 4.0f * x * (1.0f - std::fabs(x));    // parabola, peak +-1 at x = +-0.5
}

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_MATH_H
