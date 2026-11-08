// dsp/tempo.h - beats <-> seconds <-> samples, and a note-division table for sync params.
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   using namespace mpc::dsp;
//   // In the constructor: a choice param listing every division.
//   divParam_ = addParam(mpc::choiceParam("div", "Division", divisionNames(), kDivisionQuarter));
//   // In process(), with ctx.transport.bpm:
//   float beats = divisionBeats(getInt(divParam_));           // e.g. 1/8 = 0.5 beats
//   float secs  = beatsToSeconds(beats, ctx.transport.bpm);
//   float samps = beatsToSamples(beats, ctx.transport.bpm, ctx.sampleRate);
// The Transport bpm defaults to 120 when the host does not report tempo (tempoValid == false).
// Only divisionNames() allocates (it builds a std::vector<std::string>): constructor use only.

#ifndef MPC_DSP_TEMPO_H
#define MPC_DSP_TEMPO_H

#include <string>
#include <vector>

namespace mpc {
namespace dsp {

struct Division { const char* name; float beats; };   // beats are quarter notes (1 beat = 1/4 at 4/4)

// Ordered from shortest to longest. "T" = triplet (2/3), "D" = dotted (1.5x).
static const Division kDivisions[] = {
    {"1/64", 0.0625f}, {"1/32T", 0.08333f}, {"1/32", 0.125f}, {"1/16T", 0.16667f}, {"1/16", 0.25f},
    {"1/8T", 0.33333f}, {"1/16D", 0.375f}, {"1/8", 0.5f}, {"1/4T", 0.66667f}, {"1/8D", 0.75f},
    {"1/4", 1.0f}, {"1/2T", 1.33333f}, {"1/4D", 1.5f}, {"1/2", 2.0f}, {"1/2D", 3.0f},
    {"1/1", 4.0f}, {"2/1", 8.0f}
};
constexpr int kNumDivisions = (int)(sizeof(kDivisions) / sizeof(kDivisions[0]));
constexpr int kDivisionSixteenth = 4, kDivisionEighth = 7, kDivisionQuarter = 10, kDivisionHalf = 13, kDivisionBar = 15;

inline float divisionBeats(int index)
{
    if (index < 0) index = 0;
    if (index >= kNumDivisions) index = kNumDivisions - 1;
    return kDivisions[index].beats;
}
inline const char* divisionName(int index)
{
    if (index < 0) index = 0;
    if (index >= kNumDivisions) index = kNumDivisions - 1;
    return kDivisions[index].name;
}
// For choiceParam(). Allocates: call from the constructor only.
inline std::vector<std::string> divisionNames()
{
    std::vector<std::string> v;
    for (int i = 0; i < kNumDivisions; ++i) v.push_back(kDivisions[i].name);
    return v;
}

inline float safeBpm(double bpm) { return (bpm > 1.0 && bpm < 999.0) ? (float)bpm : 120.0f; }
inline float beatsToSeconds(float beats, double bpm) { return beats * 60.0f / safeBpm(bpm); }
inline float secondsToBeats(float seconds, double bpm) { return seconds * safeBpm(bpm) / 60.0f; }
inline float beatsToSamples(float beats, double bpm, double sampleRate) { return beatsToSeconds(beats, bpm) * (float)sampleRate; }
inline float samplesToBeats(float samples, double bpm, double sampleRate) { return secondsToBeats(samples / (float)sampleRate, bpm); }
inline float beatsToHz(float beats, double bpm) { const float s = beatsToSeconds(beats, bpm); return s > 0.0f ? 1.0f / s : 0.0f; }

// Position helpers for sequencer-style plugins (ppq = quarter notes since song start).
inline double ppqToStep(double ppq, float stepBeats) { return ppq / (double)stepBeats; }
// Phase 0..1 inside a cycle of `beats` length at song position ppq.
inline float ppqPhase(double ppq, float beats)
{
    if (beats <= 0.0f) return 0.0f;
    double cycles = ppq / (double)beats;
    cycles -= (double)(long long)cycles;
    if (cycles < 0.0) cycles += 1.0;
    return (float)cycles;
}

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_TEMPO_H
