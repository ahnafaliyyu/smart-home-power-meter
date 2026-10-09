#ifndef TINYML_INFERENCE_H
#define TINYML_INFERENCE_H

#include <Arduino.h>
#include "tinyml_model.h"

// Fallback konstanta jika belum terdefinisi di tinyml_model.h
#ifndef N_INPUT
#define N_INPUT 8
#endif

#ifndef N_HIDDEN1
#define N_HIDDEN1 24
#endif

#ifndef N_HIDDEN2
#define N_HIDDEN2 12
#endif

#ifndef N_OUTPUT
#define N_OUTPUT 4
#endif

#ifndef LEAKY_SLOPE
#define LEAKY_SLOPE 0.01f
#endif

// Fungsi aktivasi LeakyReLU
inline float leaky_relu(float x) {
  return (x > 0.0f) ? x : (LEAKY_SLOPE * x);
}

/**
 * Forward-pass inferensi model AR-MLP 8 -> 24 -> 12 -> 4
 * @param raw_inputs Array float berukuran 8 (fitur fisik mentah)
 *        [x0..x3: lag 1 jam, x4: baseline 6 jam, x5: lag 24 jam, x6: hour_sin, x7: hour_cos]
 * @param out_predictions Array float berukuran 4 (hasil proyeksi energi Q1-Q4 dalam satuan kWh)
 */
inline void tinyml_predict(const float* raw_inputs, float* out_predictions) {
  float norm_input[N_INPUT];
  float h1[N_HIDDEN1];
  float h2[N_HIDDEN2];

  // 1. Z-Score Normalization untuk Fitur Masukan (X)
  for (int i = 0; i < N_INPUT; i++) {
    norm_input[i] = (raw_inputs[i] - FEATURE_MEAN[i]) / (FEATURE_STD[i] + 1e-7f);
  }

  // 2. Hidden Layer 1 (Dense 8 -> 24) + LeakyReLU
  for (int j = 0; j < N_HIDDEN1; j++) {
    float sum = B_LAYER1[j];
    for (int i = 0; i < N_INPUT; i++) {
      sum += norm_input[i] * W_LAYER1[i * N_HIDDEN1 + j];
    }
    h1[j] = leaky_relu(sum);
  }

  // 3. Hidden Layer 2 (Dense 24 -> 12) + LeakyReLU
  for (int j = 0; j < N_HIDDEN2; j++) {
    float sum = B_LAYER2[j];
    for (int i = 0; i < N_HIDDEN1; i++) {
      sum += h1[i] * W_LAYER2[i * N_HIDDEN2 + j];
    }
    h2[j] = leaky_relu(sum);
  }

  // 4. Output Layer (Dense 12 -> 4) + De-normalisasi Target (Y) & Penyesuaian Urutan
  float raw_outputs[N_OUTPUT];
  for (int j = 0; j < N_OUTPUT; j++) {
    float sum = B_LAYER3[j];
    for (int i = 0; i < N_HIDDEN2; i++) {
      sum += h2[i] * W_LAYER3[i * N_OUTPUT + j];
    }

    // De-normalisasi kembali ke skala fisik riil (kWh)
    float y_kwh = (sum * TARGET_STD[j]) + TARGET_MEAN[j];

    // Pembatasan fisis: energi konsumsi tidak boleh negatif
    raw_outputs[j] = (y_kwh < 0.0f) ? 0.0f : y_kwh;
  }

  // Koreksi Pemetaan Urutan Horizon (Menjamin Q1 sampai Q4 tersusun benar secara kronologis)
  out_predictions[0] = raw_outputs[0]; // Q1: 0-6 Jam
  out_predictions[1] = raw_outputs[1]; // Q2: 6-12 Jam
  out_predictions[2] = raw_outputs[2]; // Q3: 12-18 Jam
  out_predictions[3] = raw_outputs[3]; // Q4: 18-24 Jam
}

#endif // TINYML_INFERENCE_H