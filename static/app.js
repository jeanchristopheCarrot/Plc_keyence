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
  const register = registerInput.value.trim();
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
    appendLog(`Read failed for ${register}: ${error.message}`);
  }
}

async function handleWrite() {
  const register = registerInput.value.trim();
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
    appendLog(`Write failed for ${register}: ${error.message}`);
  }
}

async function loadSimulatorRegisters() {
  try {
    const response = await fetch("/api/simulator/registers");
    const data = await response.json();
    simulatorTableBody.innerHTML = "";
    Object.entries(data.registers).forEach(([register, value]) => {
      const row = document.createElement("tr");
      const registerCell = document.createElement("td");
      const valueCell = document.createElement("td");
      registerCell.textContent = register;
      valueCell.textContent = value;
      row.appendChild(registerCell);
      row.appendChild(valueCell);
      simulatorTableBody.appendChild(row);
    });
  } catch (error) {
    appendLog(`Failed to load simulator snapshot: ${error.message}`);
  }
}

modeInputs.forEach((input) => input.addEventListener("change", renderMode));
readBtn.addEventListener("click", handleRead);
writeBtn.addEventListener("click", handleWrite);
refreshSimulatorBtn.addEventListener("click", loadSimulatorRegisters);

renderMode();
loadSimulatorRegisters();
