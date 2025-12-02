#include <Arduino.h>

#define STEP_PIN 2
#define DIR_PIN 3
#define ENA_PIN 4
#define BUT_PIN 6

#define MICRO_STEP 1
#define STEP_REV 200
#define GEAR_RATIO 5.18

const int STEPS_PER_REV = STEP_REV * MICRO_STEP * GEAR_RATIO;
const int STEP_INTERVAL = 300;

void setup() {
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);
  pinMode(BUT_PIN, INPUT);

  digitalWrite(DIR_PIN, HIGH);  // HIGH = one direction, LOW = reverse
  digitalWrite(ENA_PIN, LOW); // HIGH = NOT ACTIVE, LOW = ACTIVE
}

void loop() {

  if (digitalRead(BUT_PIN) == HIGH) {
    digitalWrite(ENA_PIN, LOW);
    digitalWrite(STEP_PIN, HIGH);
    delayMicroseconds(STEP_INTERVAL);
    digitalWrite(STEP_PIN, LOW);
    delayMicroseconds(STEP_INTERVAL);
  } else {
    digitalWrite(ENA_PIN, HIGH);
  }

}
