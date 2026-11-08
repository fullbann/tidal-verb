// dsp/adsr.h - ADSR envelope with analogue-style exponential curves.
//
// SPDX-License-Identifier: MIT
//
// Usage:
//   mpc::dsp::ADSR env;
//   prepare():   env.setSampleRate(sr);
//   per block:   env.set(attackMs, decayMs, sustain01, releaseMs);   // cheap enough per block
//   note on:     env.noteOn();            // retriggers from the current level (no click)
//   note off:    env.noteOff();           // goes to Release from wherever it is
//   per sample:  float a = env.process(); // 0..1
//   voice free?  if (!env.isActive()) ...  // true once release has decayed to silence
//   panic:       env.reset();             // instant silence
// No allocation. Curves: attack is a fast-start exponential (like a 1-pole charging a cap),
// decay/release are exponential towards the target.

#ifndef MPC_DSP_ADSR_H
#define MPC_DSP_ADSR_H

#include <cmath>

namespace mpc {
namespace dsp {

class ADSR
{
public:
    enum class Stage { Idle = 0, Attack, Decay, Sustain, Release };

    void setSampleRate(double sr) { sr_ = (float)sr; set(attackMs_, decayMs_, sustain_, releaseMs_); }

    void set(float attackMs, float decayMs, float sustain, float releaseMs)
    {
        attackMs_ = attackMs; decayMs_ = decayMs; sustain_ = sustain < 0.0f ? 0.0f : (sustain > 1.0f ? 1.0f : sustain); releaseMs_ = releaseMs;
        attackCoef_  = coef(attackMs,  kAttackRatio);
        attackBase_  = (1.0f + kAttackRatio) * (1.0f - attackCoef_);
        decayCoef_   = coef(decayMs,   kDecayRatio);
        decayBase_   = (sustain_ - kDecayRatio) * (1.0f - decayCoef_);
        releaseCoef_ = coef(releaseMs, kDecayRatio);
        releaseBase_ = -kDecayRatio * (1.0f - releaseCoef_);
    }
    void setAttack(float ms)  { set(ms, decayMs_, sustain_, releaseMs_); }
    void setDecay(float ms)   { set(attackMs_, ms, sustain_, releaseMs_); }
    void setSustain(float s)  { set(attackMs_, decayMs_, s, releaseMs_); }
    void setRelease(float ms) { set(attackMs_, decayMs_, sustain_, ms); }

    void noteOn() { stage_ = Stage::Attack; }                 // keeps current level -> smooth retrigger
    void noteOff() { if (stage_ != Stage::Idle) stage_ = Stage::Release; }
    void reset() { stage_ = Stage::Idle; level_ = 0.0f; }

    bool isActive() const { return stage_ != Stage::Idle; }
    bool isReleasing() const { return stage_ == Stage::Release; }
    bool isHeld() const { return stage_ == Stage::Attack || stage_ == Stage::Decay || stage_ == Stage::Sustain; }
    Stage stage() const { return stage_; }
    float level() const { return level_; }

    float process()
    {
        switch (stage_)
        {
            case Stage::Idle: break;
            case Stage::Attack:
                level_ = attackBase_ + level_ * attackCoef_;
                if (level_ >= 1.0f) { level_ = 1.0f; stage_ = Stage::Decay; }
                break;
            case Stage::Decay:
                level_ = decayBase_ + level_ * decayCoef_;
                if (level_ <= sustain_) { level_ = sustain_; stage_ = Stage::Sustain; }
                break;
            case Stage::Sustain:
                level_ = sustain_;
                break;
            case Stage::Release:
                level_ = releaseBase_ + level_ * releaseCoef_;
                if (level_ <= kSilence) { level_ = 0.0f; stage_ = Stage::Idle; }
                break;
        }
        return level_;
    }

private:
    static constexpr float kAttackRatio = 0.3f;      // overshoot target: higher = more linear attack
    static constexpr float kDecayRatio  = 0.0001f;   // undershoot target: smaller = more exponential
    static constexpr float kSilence     = 0.0005f;   // -66 dB: voice is considered finished

    float coef(float ms, float targetRatio) const
    {
        const float samples = ms * 0.001f * sr_;
        if (samples < 1.0f) return 0.0f;
        return std::exp(-std::log((1.0f + targetRatio) / targetRatio) / samples);
    }

    float sr_ = 44100.0f;
    float attackMs_ = 5.0f, decayMs_ = 100.0f, sustain_ = 0.8f, releaseMs_ = 100.0f;
    float attackCoef_ = 0.0f, attackBase_ = 1.3f, decayCoef_ = 0.0f, decayBase_ = 0.8f, releaseCoef_ = 0.0f, releaseBase_ = 0.0f;
    Stage stage_ = Stage::Idle;
    float level_ = 0.0f;
};

} // namespace dsp
} // namespace mpc

#endif // MPC_DSP_ADSR_H
