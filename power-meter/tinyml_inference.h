#ifndef TINYML_INFERENCE_H
#define TINYML_INFERENCE_H

#include <Arduino.h>
#include "tinyml_model.h"

// Fungsi aktivasi LeakyReLU (negative_slope = 0.01)
inline float leaky_relu(float x) {
  return (x > 0.0f) ? x : (0.01f * x);
}

// Forward-pass inferensi model AR-MLP 8 -> 16 -> 8 -> 4
inline void tinyml_predict(const float* raw_inputs, float* out_predictions) {
  float norm_input[8];
  float h1[16];
  float h2[8];

  // 1. Z-Score Normalization
  for (int i = 0; i < 8; i++) {
    norm_input[i] = (raw_inputs[i] - FEATURE_MEAN[i]) / (FEATURE_STD[i] + 1e-7f);
  }

  // 2. Hidden Layer 1 (8 -> 16) + LeakyReLU
  for (int j = 0; j < 16; j++) {
    float sum = B_LAYER1[j];
    for (int i = 0; i < 8; i++) {
      sum += norm_input[i] * W_LAYER1[i * 16 + j];
    }
    h1[j] = leaky_relu(sum);
  }

  // 3. Hidden Layer 2 (16 -> 8) + LeakyReLU
  for (int j = 0; j < 8; j++) {
    float sum = B_LAYER2[j];
    for (int i = 0; i < 16; i++) {
      sum += h1[i] * W_LAYER2[i * 8 + j];
    }
    h2[j] = leaky_relu(sum);
  }

  // 4. Output Layer (8 -> 4) + Linear Activation & Non-negative Clipping
  for (int j = 0; j < 4; j++) {
    float sum = B_LAYER3[j];
    for (int i = 0; i < 8; i++) {
      sum += h2[i] * W_LAYER3[i * 4 + j];
    }
    out_predictions[j] = (sum < 0.0f) ? 0.0f : sum;
  }
}

#endif