const $ = (id) => document.getElementById(id);
const API_BASE = window.WEATHER_API_BASE || window.location.origin;
const SELECTED_CITY_STORAGE_KEY = "weatherSelectedCityId";
let cityRecords = [];
let editingCityId = "";

function price(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(4);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function table(headers, rows) {
  const head = `<tr>${headers.map((item) => `<th>${escapeHtml(item)}</th>`).join("")}</tr>`;
  const body = rows.length
    ? rows.join("")
    : `<tr><td colspan="${headers.length}">暂无数据</td></tr>`;
  return `<table>${head}${body}</table>`;
}

function showNotice(message, isError = false) {
  const notice = $("notice");
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.style.borderColor = isError ? "#fecdca" : "#fed7aa";
  notice.style.color = isError ? "#b42318" : "#b54708";
  notice.style.background = isError ? "#fef3f2" : "#fff7ed";
}

function rememberCityId(cityId) {
  try {
    if (cityId) {
      window.localStorage.setItem(SELECTED_CITY_STORAGE_KEY, cityId);
    } else {
      window.localStorage.removeItem(SELECTED_CITY_STORAGE_KEY);
    }
  } catch {
    // localStorage can be unavailable in private or embedded contexts.
  }
}

function savedCityId() {
  try {
    return window.localStorage.getItem(SELECTED_CITY_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function cityRecordById(cityId) {
  return cityRecords.find((city) => city.cityId === cityId) || null;
}

function setField(id, value) {
  $(id).value = value ?? "";
}

function fillCityForm(city) {
  if (!city) return;
  setField("cityIdInput", city.cityId);
  setField("cityNameInput", city.name);
  setField("latitude", city.latitude);
  setField("longitude", city.longitude);
  setField("timezone", city.timezone);
  setField("cityUnit", city.settlementUnit || "F");
  setField("settlementStation", city.settlementStation);
  setField("stationId", city.stationId);
  setField("forecastGranularity", city.forecastGranularity || "city");
  setField("elevation", city.elevation);
  setField("cellSelection", city.cellSelection);
}

function renderEditingCityLabel() {
  const city = cityRecordById(editingCityId);
  $("editingCityLabel").textContent = city
    ? `正在编辑：${city.name} / ${city.cityId}`
    : "正在新建";
}

function renderCityList(activeCityId = editingCityId) {
  const rows = cityRecords.map((city) => {
    const selectedClass = city.cityId === activeCityId ? ' class="selected"' : "";
    return `<tr${selectedClass}>
      <td>${escapeHtml(city.cityId)}</td>
      <td>${escapeHtml(city.name)}</td>
      <td>${price(city.latitude)}, ${price(city.longitude)}</td>
      <td>${escapeHtml(city.timezone || "-")}</td>
      <td>${escapeHtml(city.settlementUnit || "-")}</td>
      <td>${escapeHtml(city.forecastGranularity || "city")}</td>
      <td>${escapeHtml(city.settlementStation || "-")}</td>
      <td>
        <button class="secondary table-button city-edit-button" type="button" data-city-id="${escapeHtml(city.cityId)}">编辑</button>
      </td>
    </tr>`;
  });
  $("cityList").innerHTML = table(
    ["城市 ID", "名称", "坐标", "时区", "单位", "粒度", "结算站", "操作"],
    rows,
  );
  document.querySelectorAll(".city-edit-button").forEach((button) => {
    button.addEventListener("click", () => setEditingCity(button.dataset.cityId));
  });
}

function setEditingCity(cityId) {
  const city = cityRecordById(cityId);
  if (!city) return;
  editingCityId = city.cityId;
  fillCityForm(city);
  $("cityIdInput").readOnly = true;
  rememberCityId(city.cityId);
  renderCityList(city.cityId);
  renderEditingCityLabel();
}

function cityEditorPayload() {
  return {
    editingCityId,
    cityId: $("cityIdInput").value,
    name: $("cityNameInput").value,
    latitude: $("latitude").value,
    longitude: $("longitude").value,
    timezone: $("timezone").value,
    settlementUnit: $("cityUnit").value,
    settlementStation: $("settlementStation").value,
    stationId: $("stationId").value,
    forecastGranularity: $("forecastGranularity").value,
    elevation: $("elevation").value,
    cellSelection: $("cellSelection").value,
  };
}

function stationLookupPayload() {
  return {
    settlementStation: $("settlementStation").value,
    stationId: $("stationId").value,
    limit: 5,
  };
}

function applyStationLookup(station) {
  if (!station) return;
  setField("latitude", station.latitude);
  setField("longitude", station.longitude);
  if (station.timezone) {
    setField("timezone", station.timezone);
  }
  if (station.elevation !== null && station.elevation !== undefined) {
    setField("elevation", station.elevation);
  }
  if (!$("stationId").value && station.stationId) {
    setField("stationId", station.stationId);
  }
  if (!$("settlementStation").value && station.name) {
    setField("settlementStation", station.name);
  }
  if (!$("cityNameInput").value && station.name) {
    setField("cityNameInput", station.name);
  }
  $("forecastGranularity").value = "station";
  if (!$("cellSelection").value) {
    $("cellSelection").value = "nearest";
  }
}

async function readJsonResponse(response, fallbackMessage) {
  const text = await response.text();
  let result = {};
  if (text) {
    try {
      result = JSON.parse(text);
    } catch {
      result = { error: text };
    }
  }
  if (!response.ok || result.error) {
    throw new Error(result.error || fallbackMessage);
  }
  return result;
}

async function lookupStationCoordinates({ force = false } = {}) {
  const hasStationText = $("settlementStation").value.trim() || $("stationId").value.trim();
  if (!hasStationText) return;
  if (!force && $("latitude").value && $("longitude").value) return;

  const button = $("lookupStationButton");
  button.disabled = true;
  if (force) {
    showNotice("正在解析结算站坐标...");
  }
  try {
    const response = await fetch(`${API_BASE}/api/station-lookup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(stationLookupPayload()),
    });
    const result = await readJsonResponse(response, "解析结算站失败");
    applyStationLookup(result.station);
    const stationName = result.station?.name || $("settlementStation").value || $("stationId").value;
    showNotice(`已匹配结算站坐标：${stationName}`);
  } catch (error) {
    if (force) {
      showNotice(error instanceof Error ? error.message : String(error), true);
    }
  } finally {
    button.disabled = false;
  }
}

async function loadCities(selectedCityId = savedCityId()) {
  try {
    const response = await fetch(`${API_BASE}/api/cities`);
    const result = await readJsonResponse(response, "读取城市失败");
    cityRecords = result.cities || [];
    const selected = cityRecords.some((city) => city.cityId === selectedCityId)
      ? selectedCityId
      : cityRecords[0]?.cityId;
    if (selected) {
      setEditingCity(selected);
    } else {
      renderCityList("");
      renderEditingCityLabel();
    }
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  }
}

async function saveCity() {
  const button = $("saveCityButton");
  button.disabled = true;
  showNotice("正在保存城市配置...");
  try {
    const response = await fetch(`${API_BASE}/api/cities`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cityEditorPayload()),
    });
    const result = await readJsonResponse(response, "保存城市失败");
    cityRecords = result.cities || [];
    await loadCities(result.city?.cityId);
    showNotice("城市配置已保存。");
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

function newCity() {
  editingCityId = "";
  rememberCityId("");
  $("cityIdInput").readOnly = false;
  setField("cityIdInput", "");
  setField("cityNameInput", "");
  setField("latitude", "");
  setField("longitude", "");
  setField("timezone", "auto");
  setField("cityUnit", "F");
  setField("settlementStation", "");
  setField("stationId", "");
  setField("forecastGranularity", "city");
  setField("elevation", "");
  setField("cellSelection", "");
  renderCityList("");
  renderEditingCityLabel();
  showNotice("填写新城市后点击保存城市。");
}

async function init() {
  $("newCityButton").addEventListener("click", newCity);
  $("saveCityButton").addEventListener("click", saveCity);
  $("lookupStationButton").addEventListener("click", () => lookupStationCoordinates({ force: true }));
  $("settlementStation").addEventListener("blur", () => lookupStationCoordinates());
  $("stationId").addEventListener("blur", () => lookupStationCoordinates());
  await loadCities();
}

void init();
