#include <AccelStepper.h>

#define STEP_PIN 2
#define DIR_PIN 3
#define ENA_PIN 4
#define BUT_PIN 6
#define POT_PIN A0

#define MICRO_STEP 1
#define STEP_REV 200
#define GEAR_RATIO 5.18

// ----------------------------
// Stepper Setup (DRIVER = STEP+DIR)
// ----------------------------
AccelStepper stepper(AccelStepper::DRIVER, STEP_PIN, DIR_PIN);

// ----------------------------
// Tunable Parameters
// ----------------------------
const float MIN_SPEED = 10;     // steps/sec (prevents stall)
const float MAX_SPEED = 6000;   // steps/sec (safe for NEMA17 + TB6600)
const float ACCEL     = 50;   // steps/sec^2
const int   DEADBAND  = 10;     // ADC noise deadband

int lastPot = -1;

const int STEPS_PER_REV = STEP_REV * MICRO_STEP * GEAR_RATIO;
bool run = true;

void setup() {
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);
  pinMode(BUT_PIN, INPUT);

  digitalWrite(DIR_PIN, HIGH);  // HIGH = one direction, LOW = reverse
  digitalWrite(ENA_PIN, LOW); // HIGH = NOT ACTIVE, LOW = ACTIVE

  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);
  stepper.setMinPulseWidth(5); // TB6600 needs >= 2.5us, use 5us for safety
}

void loop() {
  int potValue = analogRead(POT_PIN); // 0-1023
  float speed = map(potValue, 0, 1023, MIN_SPEED, MAX_SPEED);

  if ((digitalRead(BUT_PIN) == HIGH) && !run) {
    run = true;
    delay(1000);
    digitalWrite(ENA_PIN, LOW);
  } else if (digitalRead(BUT_PIN) == HIGH && run) {
    delay(1000);
    run = false;
    digitalWrite(ENA_PIN, HIGH);
  }

  if (run) {
    stepper.setSpeed(speed);
    stepper.runSpeed();
  }

}
