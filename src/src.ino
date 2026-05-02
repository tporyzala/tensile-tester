#include <AccelStepper.h>

#define STEP_PIN 12
#define DIR_PIN  11
#define ENA_PIN  10

#define BUT_PIN  2
#define POT_PIN  A1

#define MICRO_STEP 2
#define STEP_REV   200
#define GEAR_RATIO 19.203

// Stepper setup (STEP + DIR driver)
AccelStepper stepper(AccelStepper::DRIVER, STEP_PIN, DIR_PIN);

// Tunable parameters
const float MIN_SPEED = 10;        // steps/sec (avoid stall)
const float MAX_SPEED = 6000;     // steps/sec
const float ACCEL     = 20000;       // steps/sec^2

const long FAR_TARGET = 1000000000L;

int buttonState = 0;
int potValue    = 0;
bool motorEnabled = false;

void setup() {

  pinMode(ENA_PIN, OUTPUT);
  pinMode(BUT_PIN, INPUT_PULLUP);

  digitalWrite(DIR_PIN, HIGH);     // set direction once
  digitalWrite(ENA_PIN, HIGH);     // enable driver

  stepper.setAcceleration(ACCEL);
  stepper.setMaxSpeed(0);          // start fully stopped
  stepper.setCurrentPosition(0);
  stepper.moveTo(FAR_TARGET);      // allows continuous motion
}

void loop() {

  // Docile steppers for leveling
  if (false) {
    while (1==1) {
      digitalWrite(ENA_PIN,LOW);
    }
  }
  
  buttonState = digitalRead(BUT_PIN);
  potValue = analogRead(POT_PIN);

  float speed = map(potValue, 1023, 0, MIN_SPEED, MAX_SPEED);

  if (buttonState == LOW) {
    // Button pressed
    if (!motorEnabled) {
      stepper.moveTo(stepper.currentPosition() - FAR_TARGET);
      motorEnabled = true;
    }
    stepper.setMaxSpeed(speed);
  } else {
    // Button released
    if (motorEnabled) {
      stepper.stop();        // only issue once
      motorEnabled = false;
    }
  }

  stepper.run();
}
