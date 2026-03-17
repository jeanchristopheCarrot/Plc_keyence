const modeInputs = [...document.querySelectorAll('input[name="mode"]')];
const tcpSettings = document.getElementById("tcpSettings");
const protocolSettings = document.getElementById("protocolSettings");
const registerInput = document.getElementById("register");
const writeValueInput = document.getElementById("writeValue");
const lastReadValue = document.getElementById("lastReadValue");
const logElement = document.getElementById("log");

const hostInput = document.getElementById("host");
const portInput = document.getElementById("port");
const timeoutInput = document.getElementById("timeoutSeconds");
const encodingInput = document.getElementById("encoding");
const readTemplateInput = document.getElementById("readTemplate");
const writeTemplateInput = document.getElementById("writeTemplate");
const terminatorInput = document.getElementById("terminator");

const readBtn = document.getElementById("readBtn");
const writeBtn = document.getElementById("writeBtn");
const refreshSimulatorBtn = document.getElementById("refreshSimulatorBtn");
const simulatorTableBody = document.querySelector("#simulatorTable tbody");
const registerFileInput = document.getElementById("registerFileInput");
const uploadRegisterFileBtn = document.getElementById("uploadRegisterFileBtn");
const importStatus = document.getElementById("importStatus");
const localSimulatorRegisters = {
  DM0: 0,
  DM1: 100,
  DM2: 200,
  R0: 0,
  R1: 1,
  R2: 0,
};

function normalizeRegister(register) {
  return (register || "").trim().toUpperCase();
}

function currentMode() {
  const selected = modeInputs.find((input) => input.checked);
  return selected ? selected.value : "simulator";
}

function appendLog(message, details = null) {
  const timestamp = new Date().toISOString();
  let line = `[${timestamp}] ${message}`;
  if (details) {
    line += `\n${JSON.stringify(details, null, 2)}`;
  }
  logElement.textContent = `${line}\n\n${logElement.textContent}`.trim();
}

function getTcpConfig() {
  return {
    connection: {
      host: hostInput.value.trim(),
      port: Number(portInput.value),
      timeoutSeconds: Number(timeoutInput.value),
    },
    protocol: {
      readTemplate: readTemplateInput.value,
      writeTemplate: writeTemplateInput.value,
      terminator: terminatorInput.value,
      encoding: encodingInput.value.trim() || "ascii",
    },
  };
}

function renderMode() {
  const tcpMode = currentMode() === "tcp";
  tcpSettings.classList.toggle("hidden", !tcpMode);
  protocolSettings.classList.toggle("hidden", !tcpMode);
}

async function apiCall(endpoint, payload) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

async function handleRead() {
  const register = normalizeRegister(registerInput.value);
  if (!register) {
    appendLog("Read blocked: register is required.");
    return;
  }

  const payload = { mode: currentMode(), register };
  if (payload.mode === "tcp") {
    Object.assign(payload, getTcpConfig());
  }

  try {
    const data = await apiCall("/api/read", payload);
    lastReadValue.textContent =
      data.value === null || data.value === undefined ? "N/A" : data.value;
    appendLog(`Read ${register} (${payload.mode})`, data);
    if (payload.mode === "simulator") {
      await loadSimulatorRegisters();
    }
  } catch (error) {
    if (payload.mode === "simulator") {
      const value = Object.prototype.hasOwnProperty.call(
        localSimulatorRegisters,
        register
      )
        ? localSimulatorRegisters[register]
        : 0;
      lastReadValue.textContent = value;
      appendLog(
        `Read ${register} (simulator local fallback)`,
        {
          value,
          reason: error.message,
        }
      );
      await loadSimulatorRegisters();
      return;
    }
    appendLog(`Read failed for ${register}: ${error.message}`);
  }
}

async function handleWrite() {
  const register = normalizeRegister(registerInput.value);
  if (!register) {
    appendLog("Write blocked: register is required.");
    return;
  }

  const value = Number(writeValueInput.value);
  if (!Number.isInteger(value)) {
    appendLog("Write blocked: value must be an integer.");
    return;
  }

  const payload = { mode: currentMode(), register, value };
  if (payload.mode === "tcp") {
    Object.assign(payload, getTcpConfig());
  }

  try {
    const data = await apiCall("/api/write", payload);
    appendLog(`Wrote ${value} to ${register} (${payload.mode})`, data);
    if (payload.mode === "simulator") {
      await loadSimulatorRegisters();
    }
  } catch (error) {
    if (payload.mode === "simulator") {
      localSimulatorRegisters[register] = value;
      appendLog(
        `Wrote ${value} to ${register} (simulator local fallback)`,
        {
          reason: error.message,
        }
      );
      await loadSimulatorRegisters();
      return;
    }
    appendLog(`Write failed for ${register}: ${error.message}`);
  }
}

async function loadSimulatorRegisters() {
  try {
    const response = await fetch("/api/simulator/registers");
    const data = await response.json();
    simulatorTableBody.innerHTML = "";
    Object.assign(localSimulatorRegisters, data.registers || {});
    renderSimulatorRows(localSimulatorRegisters);
  } catch (error) {
    renderSimulatorRows(localSimulatorRegisters);
    appendLog(
      "Using simulator local fallback snapshot",
      { reason: error.message }
    );
  }
}

async function handleRegisterFileUpload() {
  const [file] = registerFileInput.files || [];
  if (!file) {
    appendLog("Upload blocked: select a .zip or .csv file first.");
    return;
  }

  uploadRegisterFileBtn.disabled = true;
  uploadRegisterFileBtn.textContent = "Uploading...";

  try {
    const response = await fetch("/api/upload-register-file", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": file.name,
      },
      body: file,
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Upload failed");
    }

    importStatus.textContent = `${data.loadedRegisters} registers loaded from ${data.sourceFile}`;
    appendLog("Simulator register import completed", data);
    await loadSimulatorRegisters();
  } catch (error) {
    importStatus.textContent = `Import failed: ${error.message}`;
    appendLog(`Simulator register import failed: ${error.message}`);
  } finally {
    uploadRegisterFileBtn.disabled = false;
    uploadRegisterFileBtn.textContent = "Upload and Load Simulator";
  }
}

function renderSimulatorRows(registers) {
  simulatorTableBody.innerHTML = "";
  Object.entries(registers)
    .sort(([a], [b]) => a.localeCompare(b))
    .forEach(([register, value]) => {
      const row = document.createElement("tr");
      const registerCell = document.createElement("td");
      const valueCell = document.createElement("td");
      registerCell.textContent = register;
      valueCell.textContent = value;
      row.appendChild(registerCell);
      row.appendChild(valueCell);
      simulatorTableBody.appendChild(row);
    });
}

modeInputs.forEach((input) => input.addEventListener("change", renderMode));
readBtn.addEventListener("click", handleRead);
writeBtn.addEventListener("click", handleWrite);
refreshSimulatorBtn.addEventListener("click", loadSimulatorRegisters);
uploadRegisterFileBtn.addEventListener("click", handleRegisterFileUpload);

renderMode();
loadSimulatorRegisters();
