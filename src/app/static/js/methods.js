(function () {
  function syncStepForm(form) {
    const selector = form.querySelector("[data-step-type-select]");
    if (!selector) {
      return;
    }

    const isRamp = selector.value === "RAMP_TO_LOAD";
    form.querySelectorAll("[data-step-field='ramp']").forEach((field) => {
      field.hidden = !isRamp;
      const input = field.querySelector("input");
      if (input) {
        input.required = isRamp;
        input.disabled = !isRamp;
      }
    });
    form.querySelectorAll("[data-step-field='hold']").forEach((field) => {
      field.hidden = isRamp;
      const input = field.querySelector("input");
      if (input) {
        input.required = !isRamp;
        input.disabled = isRamp;
      }
    });
  }

  document.querySelectorAll(".step-form").forEach((form) => {
    syncStepForm(form);
    const selector = form.querySelector("[data-step-type-select]");
    selector?.addEventListener("change", () => syncStepForm(form));
  });
})();
