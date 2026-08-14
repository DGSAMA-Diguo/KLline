(() => {
  "use strict";

  const RECORD_SIZE = 22;
  // 15 根日 K 保证相似片段和详情窗口不少于半个月。
  const MIN_SELECTION = 15;
  const MAX_SELECTION = 400;
  // 初始默认显示的 K 线根数，避免一次性展示全部历史导致图表拥挤。
  const DEFAULT_VIEW_BARS = 120;
  const INDICATOR_WARMUP = 35;
  const RESULT_COUNT = 10;
  const DEFAULT_SIMILARITY_FILTERS = ["kline", "volume", "macd"];
  // 为所有可选组合分别保留前十名，切换筛选条件时无需保存全市场结果。
  const SIMILARITY_FILTER_COMBINATIONS = [
    ["kline"],
    ["volume"],
    ["macd"],
    ["kline", "volume"],
    ["kline", "macd"],
    ["volume", "macd"],
    ["kline", "volume", "macd"],
  ];
  const SIMILARITY_FILTER_LABELS = {
    kline: "K 线",
    volume: "交易量",
    macd: "MACD",
  };
  const LITTLE_ENDIAN = true;
  const LIVE_MARKET_ENDPOINT = (
    "https://push2delay.eastmoney.com/api/qt/clist/get"
  );
  const LIVE_PAGE_SIZE = 100;
  const LIVE_WORKERS = 6;
  const LIVE_REQUEST_TIMEOUT_MS = 15000;
  const LIVE_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
  const LIVE_SEARCH_MAX_AGE_MS = 60 * 1000;
  // 超过阈值且方向明确后才执行控件操作，避免页面滚动时误触。
  const GESTURE_MOVE_THRESHOLD = 12;
  const GESTURE_DIRECTION_RATIO = 1.15;
  const SCROLL_CLICK_BLOCK_MS = 700;
  // 分块解码可避免 Safari 同时保留整份二进制字符串和字节数组。
  const BASE64_DECODE_CHUNK_SIZE = 1024 * 1024;
  const FAVORITE_STORAGE_KEY = "kline-mobile-favorites-v1";
  const FAVORITE_STORAGE_VERSION = 1;
  const FAVORITE_PREVIEW_WARMUP = 120;
  const FAVORITE_STORAGE_LIMIT = 5 * 1024 * 1024;

  const state = {
    dates: [],
    stocks: [],
    stockByCode: new Map(),
    embeddedDate: "",
    marketDate: "",
    barsView: null,
    liveBars: new Map(),
    liveUpdatedAt: null,
    liveRefreshPromise: null,
    liveRefreshTimer: 0,
    target: null,
    targetIsFavorite: false,
    targetBars: [],
    targetIndicators: [],
    viewStart: 0,
    viewEnd: 0,
    selection: null,
    dragAnchor: null,
    chartGesture: null,
    searchToken: 0,
    candidateResults: [],
    results: [],
    detailStock: null,
    detailBars: [],
    detailIndicators: [],
    detailStart: 0,
    detailEnd: 0,
    favorites: [],
    selectedFavoriteId: null,
    favoritePreviewBars: [],
    favoritePreviewIndicators: [],
    favoritePreviewSelection: null,
    favoriteStorageError: "",
  };

  const elements = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function syncViewportHeight() {
    const visualHeight = window.visualViewport
      ? Number(window.visualViewport.height)
      : 0;
    const viewportHeight = Math.round(
      visualHeight
      || window.innerHeight
      || document.documentElement.clientHeight
      || 0,
    );
    if (viewportHeight > 0) {
      document.documentElement.style.setProperty(
        "--viewport-height",
        `${viewportHeight}px`,
      );
    }
  }

  // 兼容未实现 replaceChildren 和多参数 append 的安卓浏览器。
  function replaceElementChildren(element, ...children) {
    while (element.firstChild) {
      element.removeChild(element.firstChild);
    }
    for (const child of children) {
      element.appendChild(child);
    }
  }

  function appendElementChildren(element, ...children) {
    for (const child of children) {
      element.appendChild(child);
    }
  }

  function cacheElements() {
    const ids = [
      "loading-screen",
      "loading-text",
      "app",
      "market-summary",
      "market-mode",
      "refresh-live",
      "query-form",
      "stock-query",
      "load-button",
      "query-message",
      "favorites-button",
      "chart-title",
      "stock-summary",
      "main-chart",
      "chart-placeholder",
      "view-start",
      "view-end",
      "view-label",
      "selection-summary",
      "reset-view",
      "zoom-selection",
      "save-favorite",
      "search-button",
      "progress-wrap",
      "progress-bar",
      "progress-label",
      "filter-kline",
      "filter-volume",
      "filter-macd",
      "result-summary",
      "result-list",
      "detail-modal",
      "detail-title",
      "detail-summary",
      "detail-close",
      "detail-chart",
      "detail-start",
      "detail-end",
      "detail-view-label",
      "favorites-modal",
      "favorites-summary",
      "favorites-close",
      "favorites-list",
      "favorite-preview-summary",
      "favorite-preview-chart",
      "favorite-preview-placeholder",
      "favorite-search",
      "favorite-delete",
    ];
    for (const id of ids) {
      elements[id] = byId(id);
    }
  }

  function decodeBase64(base64Text) {
    if (
      typeof base64Text !== "string"
      || !base64Text.length
      || base64Text.length % 4 !== 0
    ) {
      throw new Error("移动行情 Base64 数据长度无效");
    }

    let padding = 0;
    if (base64Text.endsWith("==")) {
      padding = 2;
    } else if (base64Text.endsWith("=")) {
      padding = 1;
    }
    const byteLength = (base64Text.length / 4) * 3 - padding;
    const bytes = new Uint8Array(byteLength);
    let writeOffset = 0;

    for (
      let offset = 0;
      offset < base64Text.length;
      offset += BASE64_DECODE_CHUNK_SIZE
    ) {
      const chunk = window.atob(
        base64Text.slice(offset, offset + BASE64_DECODE_CHUNK_SIZE),
      );
      for (let index = 0; index < chunk.length; index += 1) {
        bytes[writeOffset] = chunk.charCodeAt(index);
        writeOffset += 1;
      }
    }
    if (writeOffset !== byteLength) {
      throw new Error("移动行情 Base64 数据解码不完整");
    }
    return bytes;
  }

  function initializeMarketData(payload) {
    if (!payload || payload.version !== 1) {
      throw new Error("移动行情数据版本不受支持");
    }
    if (!Array.isArray(payload.dates) || !Array.isArray(payload.stocks)) {
      throw new Error("移动行情数据结构不完整");
    }

    elements["loading-text"].textContent = "正在解压全市场 K 线";
    const bytes = decodeBase64(payload.bars);
    if (bytes.byteLength % RECORD_SIZE !== 0) {
      throw new Error("移动行情记录长度无效");
    }

    state.dates = payload.dates;
    state.embeddedDate = String(payload.date || "");
    state.marketDate = state.embeddedDate;
    state.barsView = new DataView(
      bytes.buffer,
      bytes.byteOffset,
      bytes.byteLength,
    );
    state.stocks = payload.stocks.map((item) => {
      const offset = Number(item[4]);
      const count = Number(item[5]);
      let embeddedLatestDate = "";
      if (count > 0) {
        const dateIndex = state.barsView.getUint16(
          (offset + count - 1) * RECORD_SIZE,
          LITTLE_ENDIAN,
        );
        embeddedLatestDate = state.dates[dateIndex] || "";
      }
      return {
        market: Number(item[0]),
        code: String(item[1]),
        name: String(item[2]),
        exchange: String(item[3]),
        offset,
        count,
        embeddedLatestDate,
      };
    });
    state.stockByCode.clear();
    for (const stock of state.stocks) {
      if (!state.stockByCode.has(stock.code)) {
        state.stockByCode.set(stock.code, stock);
      }
    }

    const expectedRecords = state.stocks.reduce(
      (total, stock) => total + stock.count,
      0,
    );
    if (expectedRecords * RECORD_SIZE !== bytes.byteLength) {
      throw new Error("移动行情索引与 K 线记录不一致");
    }
  }

  function readBars(stock, limit = null) {
    if (!state.barsView) {
      return [];
    }
    const available = stock.count;
    const liveBar = state.liveBars.get(stock.code) || null;
    const embeddedLatestDate = stock.embeddedLatestDate || "";
    const replacesLatest = Boolean(
      liveBar && liveBar.date === embeddedLatestDate,
    );
    const appendsLatest = Boolean(
      liveBar && liveBar.date > embeddedLatestDate,
    );
    const totalAvailable = available + (appendsLatest ? 1 : 0);
    const usedCount = limit === null
      ? totalAvailable
      : Math.min(totalAvailable, Math.max(0, limit));
    const embeddedUsedCount = Math.min(
      available,
      usedCount - (appendsLatest && usedCount > 0 ? 1 : 0),
    );
    const startInStock = available - embeddedUsedCount;
    const bars = new Array(embeddedUsedCount);

    for (
      let localIndex = 0;
      localIndex < embeddedUsedCount;
      localIndex += 1
    ) {
      const recordIndex = stock.offset + startInStock + localIndex;
      const byteOffset = recordIndex * RECORD_SIZE;
      const dateIndex = state.barsView.getUint16(
        byteOffset,
        LITTLE_ENDIAN,
      );
      bars[localIndex] = {
        date: state.dates[dateIndex] || "",
        open: state.barsView.getFloat32(
          byteOffset + 2,
          LITTLE_ENDIAN,
        ),
        high: state.barsView.getFloat32(
          byteOffset + 6,
          LITTLE_ENDIAN,
        ),
        low: state.barsView.getFloat32(
          byteOffset + 10,
          LITTLE_ENDIAN,
        ),
        close: state.barsView.getFloat32(
          byteOffset + 14,
          LITTLE_ENDIAN,
        ),
        volume: state.barsView.getFloat32(
          byteOffset + 18,
          LITTLE_ENDIAN,
        ),
      };
    }
    if (replacesLatest && bars.length) {
      bars[bars.length - 1] = { ...liveBar };
    } else if (appendsLatest && usedCount > 0) {
      bars.push({ ...liveBar });
    }
    return bars;
  }

  function latestStockDate(stock) {
    const embeddedDate = stock.embeddedLatestDate || "";
    const liveBar = state.liveBars.get(stock.code) || null;
    if (liveBar && liveBar.date >= embeddedDate) {
      return liveBar.date;
    }
    return embeddedDate;
  }

  function availableStockBarCount(stock) {
    const embeddedDate = stock.embeddedLatestDate || "";
    const liveBar = state.liveBars.get(stock.code) || null;
    return stock.count + (
      liveBar && liveBar.date > embeddedDate ? 1 : 0
    );
  }

  function normalizeFavoriteBar(value) {
    if (!value || typeof value !== "object") {
      return null;
    }
    const date = String(value.date || "");
    const open = Number(value.open);
    const high = Number(value.high);
    const low = Number(value.low);
    const close = Number(value.close);
    const volume = Number(value.volume);
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(date)
      || [open, high, low, close].some(
        (price) => !Number.isFinite(price) || price <= 0,
      )
      || !Number.isFinite(volume)
      || volume < 0
      || high < low
      || high < Math.max(open, close)
      || low > Math.min(open, close)
    ) {
      return null;
    }
    return { date, open, high, low, close, volume };
  }

  function normalizeFavorite(value) {
    if (!value || typeof value !== "object") {
      return null;
    }
    const id = Number(value.id);
    const stockValue = value.stock;
    const name = String(value.name || "").trim();
    const createdAt = String(value.createdAt || "");
    const selectionStart = Number(value.selectionStart);
    const selectionCount = Number(value.selectionCount);
    if (
      !Number.isSafeInteger(id)
      || id <= 0
      || !stockValue
      || typeof stockValue !== "object"
      || name.length < 1
      || name.length > 120
      || createdAt.length < 10
      || createdAt.length > 40
      || !Number.isInteger(selectionStart)
      || selectionStart < 0
      || !Number.isInteger(selectionCount)
      || selectionCount < MIN_SELECTION
      || selectionCount > MAX_SELECTION
      || !Array.isArray(value.contextBars)
      || value.contextBars.length < selectionCount
      || value.contextBars.length > FAVORITE_PREVIEW_WARMUP + MAX_SELECTION
      || selectionStart + selectionCount > value.contextBars.length
    ) {
      return null;
    }

    const code = String(stockValue.code || "");
    const stockName = String(stockValue.name || "").trim();
    const exchange = String(stockValue.exchange || "").trim();
    const market = Number(stockValue.market);
    if (
      !/^\d{6}$/.test(code)
      || stockName.length < 1
      || stockName.length > 80
      || exchange.length < 1
      || exchange.length > 12
      || !Number.isInteger(market)
    ) {
      return null;
    }

    const contextBars = [];
    let previousDate = "";
    for (const rawBar of value.contextBars) {
      const bar = normalizeFavoriteBar(rawBar);
      if (!bar || (previousDate && bar.date <= previousDate)) {
        return null;
      }
      previousDate = bar.date;
      contextBars.push(bar);
    }
    return {
      id,
      name,
      createdAt,
      stock: {
        market,
        code,
        name: stockName,
        exchange,
      },
      contextBars,
      selectionStart,
      selectionCount,
    };
  }

  function loadFavorites() {
    state.favoriteStorageError = "";
    try {
      const stored = window.localStorage.getItem(FAVORITE_STORAGE_KEY);
      if (!stored) {
        state.favorites = [];
        return;
      }
      if (stored.length > FAVORITE_STORAGE_LIMIT) {
        throw new Error("收藏数据超过本地存储安全上限");
      }
      const payload = JSON.parse(stored);
      if (
        !payload
        || payload.version !== FAVORITE_STORAGE_VERSION
        || !Array.isArray(payload.items)
      ) {
        throw new Error("收藏数据版本或结构无效");
      }
      const favorites = [];
      const ids = new Set();
      for (const item of payload.items) {
        const favorite = normalizeFavorite(item);
        if (favorite && !ids.has(favorite.id)) {
          ids.add(favorite.id);
          favorites.push(favorite);
        }
      }
      state.favorites = favorites;
    } catch (error) {
      state.favorites = [];
      state.favoriteStorageError = error.message || String(error);
    }
  }

  function persistFavorites(favorites) {
    const normalized = [];
    const ids = new Set();
    for (const item of favorites) {
      const favorite = normalizeFavorite(item);
      if (!favorite || ids.has(favorite.id)) {
        throw new Error("收藏数据校验失败");
      }
      ids.add(favorite.id);
      normalized.push(favorite);
    }
    const serialized = JSON.stringify({
      version: FAVORITE_STORAGE_VERSION,
      items: normalized,
    });
    if (serialized.length > FAVORITE_STORAGE_LIMIT) {
      throw new Error("收藏数量过多，本地存储空间不足");
    }
    try {
      window.localStorage.setItem(FAVORITE_STORAGE_KEY, serialized);
    } catch (_error) {
      throw new Error("当前浏览器不允许持久保存收藏");
    }
    state.favorites = normalized;
    state.favoriteStorageError = "";
  }

  function nextFavoriteId() {
    let id = Date.now();
    const usedIds = new Set(state.favorites.map((favorite) => favorite.id));
    while (usedIds.has(id)) {
      id += 1;
    }
    return id;
  }

  function selectedFavorite() {
    return state.favorites.find(
      (favorite) => favorite.id === state.selectedFavoriteId,
    ) || null;
  }

  function formatLiveDate(timestamp) {
    const seconds = Number(timestamp);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      return "";
    }
    // 使用固定东八区计算交易日期，避免设备时区影响行情归属日。
    const date = new Date((Math.trunc(seconds) + 8 * 3600) * 1000);
    const year = date.getUTCFullYear();
    const month = String(date.getUTCMonth() + 1).padStart(2, "0");
    const day = String(date.getUTCDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function formatUpdateTime(date) {
    if (!(date instanceof Date)) {
      return "";
    }
    return date.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function updateMarketStatus(extraText = "") {
    const parts = [
      `${state.stocks.length.toLocaleString("zh-CN")} 只 A 股`,
      `行情日期 ${state.marketDate}`,
    ];
    if (state.liveUpdatedAt) {
      parts.push(`更新于 ${formatUpdateTime(state.liveUpdatedAt)}`);
    }
    if (extraText) {
      parts.push(extraText);
    }
    elements["market-summary"].textContent = parts.join(" · ");
  }

  function setMarketMode(mode, text) {
    elements["market-mode"].className = `market-badge ${mode}`;
    elements["market-mode"].textContent = text;
  }

  function buildLiveMarketUrl(page) {
    const parameters = [
      ["pn", String(page)],
      ["pz", String(LIVE_PAGE_SIZE)],
      ["po", "1"],
      ["np", "1"],
      ["ut", "bd1d9ddb04089700cf9c27f6f7426281"],
      ["fltt", "2"],
      ["invt", "2"],
      ["fid", "f3"],
      ["fs", (
        "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,"
        + "m:0+t:81+s:2048"
      )],
      ["fields", "f12,f13,f2,f5,f15,f16,f17,f18,f124"],
      ["_", `${Date.now()}_${page}`],
    ];
    const query = parameters.map(([key, value]) => (
      `${encodeURIComponent(key)}=${encodeURIComponent(value)}`
    )).join("&");
    return `${LIVE_MARKET_ENDPOINT}?${query}`;
  }

  function validateLivePayload(payload) {
    if (
      !payload
      || Number(payload.rc) !== 0
      || !payload.data
    ) {
      throw new Error("实时行情响应格式无效");
    }
    return payload;
  }

  function fetchLiveJsonWithXhr(url) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("GET", url, true);
      request.timeout = LIVE_REQUEST_TIMEOUT_MS;
      request.withCredentials = false;
      request.onload = () => {
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(`行情服务返回状态 ${request.status}`));
          return;
        }
        try {
          resolve(validateLivePayload(JSON.parse(request.responseText)));
        } catch (error) {
          reject(error);
        }
      };
      request.onerror = () => {
        reject(new Error("实时行情网络请求失败"));
      };
      request.ontimeout = () => {
        reject(new Error("实时行情请求超时"));
      };
      request.send();
    });
  }

  async function fetchLiveJson(url) {
    // 荣耀及部分旧安卓 WebView 没有 fetch，使用跨域 XHR 回退。
    if (typeof window.fetch !== "function") {
      return fetchLiveJsonWithXhr(url);
    }
    const controller = typeof AbortController === "function"
      ? new AbortController()
      : null;
    const timeout = window.setTimeout(() => {
      if (controller) {
        controller.abort();
      }
    }, LIVE_REQUEST_TIMEOUT_MS);
    try {
      const response = await window.fetch(url, {
        method: "GET",
        mode: "cors",
        cache: "no-store",
        credentials: "omit",
        referrerPolicy: "no-referrer",
        signal: controller ? controller.signal : undefined,
      });
      if (!response.ok) {
        throw new Error(`行情服务返回状态 ${response.status}`);
      }
      return validateLivePayload(await response.json());
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error("实时行情请求超时");
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function fetchLivePage(page) {
    let lastError = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        return await fetchLiveJson(buildLiveMarketUrl(page));
      } catch (error) {
        lastError = error;
        if (attempt === 0) {
          await new Promise((resolve) => {
            window.setTimeout(resolve, 350);
          });
        }
      }
    }
    throw lastError || new Error(`第 ${page} 页实时行情读取失败`);
  }

  async function downloadLiveMarket() {
    const firstPayload = await fetchLivePage(1);
    const total = Number(firstPayload.data.total);
    const firstRecords = firstPayload.data.diff;
    if (
      !Number.isInteger(total)
      || total <= 0
      || total > 20000
      || !Array.isArray(firstRecords)
    ) {
      throw new Error("实时行情总数无效");
    }

    const pageCount = Math.ceil(total / LIVE_PAGE_SIZE);
    const pages = new Array(pageCount);
    pages[0] = firstRecords;
    let nextPage = 2;
    let completed = 1;

    async function worker() {
      while (nextPage <= pageCount) {
        const page = nextPage;
        nextPage += 1;
        const payload = await fetchLivePage(page);
        if (!Array.isArray(payload.data.diff)) {
          throw new Error(`第 ${page} 页实时行情格式无效`);
        }
        pages[page - 1] = payload.data.diff;
        completed += 1;
        updateMarketStatus(`正在更新 ${completed}/${pageCount}`);
      }
    }

    const workerCount = Math.min(
      LIVE_WORKERS,
      Math.max(0, pageCount - 1),
    );
    await Promise.all(
      Array.from({ length: workerCount }, () => worker()),
    );
    const records = [];
    for (const page of pages) {
      for (const record of page) {
        records.push(record);
      }
    }
    return records;
  }

  function parseLiveBar(record) {
    if (!record || typeof record !== "object") {
      return null;
    }
    const code = String(record.f12 || "");
    const stock = state.stockByCode.get(code);
    if (!stock) {
      return null;
    }
    const open = Number(record.f17);
    const high = Number(record.f15);
    const low = Number(record.f16);
    const close = Number(record.f2);
    const volume = Number(record.f5);
    const date = formatLiveDate(record.f124);
    const prices = [open, high, low, close];
    if (
      !date
      || prices.some((value) => !Number.isFinite(value) || value <= 0)
      || !Number.isFinite(volume)
      || volume < 0
      || high < low
      || high < Math.max(open, close)
      || low > Math.min(open, close)
    ) {
      return null;
    }
    return {
      stock,
      bar: {
        date,
        open,
        high,
        low,
        close,
        volume,
      },
    };
  }

  function applyLiveMarket(records) {
    const parsed = [];
    const dateCounts = new Map();
    for (const record of records) {
      const item = parseLiveBar(record);
      if (!item) {
        continue;
      }
      parsed.push(item);
      dateCounts.set(
        item.bar.date,
        (dateCounts.get(item.bar.date) || 0) + 1,
      );
    }
    const dates = [...dateCounts.entries()].sort((first, second) => (
      second[1] - first[1] || second[0].localeCompare(first[0])
    ));
    if (!dates.length) {
      throw new Error("实时行情中没有有效 A 股数据");
    }
    const marketDate = dates[0][0];
    if (state.embeddedDate && marketDate < state.embeddedDate) {
      throw new Error("实时行情日期早于内置数据");
    }

    const liveBars = new Map();
    for (const item of parsed) {
      if (item.bar.date === marketDate) {
        liveBars.set(item.stock.code, item.bar);
      }
    }
    const minimumCoverage = Math.floor(state.stocks.length * 0.7);
    if (liveBars.size < minimumCoverage) {
      throw new Error(
        `实时行情覆盖不足：${liveBars.size}/${state.stocks.length}`,
      );
    }

    state.liveBars = liveBars;
    state.marketDate = marketDate;
    state.liveUpdatedAt = new Date();
    return liveBars.size;
  }

  function refreshLoadedCharts() {
    state.searchToken += 1;
    // 收藏预览使用保存时的历史 K 线，实时更新不得改写该形态。
    if (state.target && !state.targetIsFavorite) {
      const oldCount = state.targetBars.length;
      const wasShowingAll = (
        state.viewStart === 0 && state.viewEnd === oldCount
      );
      state.targetBars = readBars(state.target);
      state.targetIndicators = calculateIndicators(state.targetBars);
      elements["stock-summary"].textContent = (
        `${state.target.exchange} · ${state.targetBars[0].date} 至 `
        + `${state.targetBars[state.targetBars.length - 1].date} · `
        + `${state.targetBars.length} 根日 K`
      );
      if (wasShowingAll) {
        setView(0, state.targetBars.length, true);
      } else {
        setView(
          Math.min(state.viewStart, state.targetBars.length - 1),
          Math.min(state.viewEnd, state.targetBars.length),
          true,
        );
      }
      if (state.selection) {
        updateSelection(
          Math.min(state.selection[0], state.targetBars.length - 1),
          Math.min(state.selection[1], state.targetBars.length - 1),
        );
      }
    }

    if (state.detailStock) {
      const oldCount = state.detailBars.length;
      const wasShowingAll = (
        state.detailStart === 0 && state.detailEnd === oldCount
      );
      state.detailBars = readBars(state.detailStock);
      state.detailIndicators = calculateIndicators(state.detailBars);
      elements["detail-summary"].textContent = (
        `${state.detailStock.exchange} · ${state.detailBars[0].date} 至 `
        + `${state.detailBars[state.detailBars.length - 1].date} · `
        + `${state.detailBars.length} 根日 K`
      );
      if (wasShowingAll) {
        setDetailView(0, state.detailBars.length);
      } else {
        setDetailView(
          Math.min(state.detailStart, state.detailBars.length - 1),
          Math.min(state.detailEnd, state.detailBars.length),
        );
      }
    }

    if (state.candidateResults.length) {
      state.candidateResults = [];
      state.results = [];
      elements["result-summary"].textContent = (
        "实时行情已更新，请重新寻找相似股票"
      );
      replaceElementChildren(
        elements["result-list"],
        createEmptyState("行情已更新，原匹配结果已清除"),
      );
    }
  }

  function liveDataIsStale() {
    return (
      !state.liveUpdatedAt
      || Date.now() - state.liveUpdatedAt.getTime()
        > LIVE_SEARCH_MAX_AGE_MS
    );
  }

  function refreshLiveMarket(options = {}) {
    if (state.liveRefreshPromise) {
      return state.liveRefreshPromise;
    }
    const manual = Boolean(options.manual);
    elements["refresh-live"].disabled = true;
    setMarketMode("updating", "正在更新");
    updateMarketStatus("正在连接公开行情");

    const operation = (async () => {
      const records = await downloadLiveMarket();
      const liveCount = applyLiveMarket(records);
      refreshLoadedCharts();
      setMarketMode("online", "实时行情");
      updateMarketStatus(`${liveCount.toLocaleString("zh-CN")} 只已更新`);
      if (manual) {
        setMessage("全市场实时行情更新完成", "success");
      }
      return liveCount;
    })().catch((error) => {
      if (state.liveUpdatedAt) {
        setMarketMode("warning", "更新失败");
        updateMarketStatus("保留上次实时行情");
      } else {
        setMarketMode("offline", "内置行情");
        updateMarketStatus("联网失败，使用内置数据");
      }
      if (manual) {
        setMessage(
          `实时行情更新失败：${error.message || String(error)}`,
          "error",
        );
      }
      throw error;
    }).finally(() => {
      elements["refresh-live"].disabled = false;
      state.liveRefreshPromise = null;
    });
    state.liveRefreshPromise = operation;
    return operation;
  }

  function calculateIndicators(bars) {
    if (!bars.length) {
      return [];
    }
    const prefix = new Float64Array(bars.length + 1);
    for (let index = 0; index < bars.length; index += 1) {
      const close = bars[index].close;
      if (!Number.isFinite(close) || close <= 0) {
        throw new Error("K 线包含无效收盘价");
      }
      prefix[index + 1] = prefix[index] + close;
    }

    function movingAverage(index, period) {
      if (index + 1 < period) {
        return null;
      }
      return (
        prefix[index + 1] - prefix[index + 1 - period]
      ) / period;
    }

    let ema12 = bars[0].close;
    let ema26 = bars[0].close;
    let dea = 0;
    const indicators = new Array(bars.length);
    for (let index = 0; index < bars.length; index += 1) {
      const close = bars[index].close;
      if (index > 0) {
        ema12 += (close - ema12) * 2 / 13;
        ema26 += (close - ema26) * 2 / 27;
      }
      const dif = ema12 - ema26;
      dea += (dif - dea) * 2 / 10;
      indicators[index] = {
        ma5: movingAverage(index, 5),
        ma10: movingAverage(index, 10),
        ma20: movingAverage(index, 20),
        ma30: movingAverage(index, 30),
        ma60: movingAverage(index, 60),
        ma120: movingAverage(index, 120),
        dif,
        dea,
        macd: (dif - dea) * 2,
      };
    }
    return indicators;
  }

  function standardize(values) {
    let sum = 0;
    for (const value of values) {
      if (!Number.isFinite(value)) {
        throw new Error("指标包含无效数值");
      }
      sum += value;
    }
    const mean = sum / values.length;
    let variance = 0;
    for (const value of values) {
      const difference = value - mean;
      variance += difference * difference;
    }
    const deviation = Math.sqrt(variance / values.length);
    const result = new Float64Array(values.length);
    if (deviation < 1e-12) {
      return result;
    }
    for (let index = 0; index < values.length; index += 1) {
      result[index] = (values[index] - mean) / deviation;
    }
    return result;
  }

  function normalizeKline(bars) {
    if (bars.length < 2) {
      throw new Error("至少需要两根 K 线");
    }
    const referenceClose = bars[0].close;
    const values = new Float64Array(bars.length * 4);
    let outputIndex = 0;
    for (const bar of bars) {
      for (const price of [bar.open, bar.high, bar.low, bar.close]) {
        if (!Number.isFinite(price) || price <= 0) {
          throw new Error("K 线包含无效价格");
        }
        values[outputIndex] = Math.log(price / referenceClose);
        outputIndex += 1;
      }
    }
    const normalized = standardize(values);
    let lengthSquared = 0;
    for (const value of normalized) {
      lengthSquared += value * value;
    }
    if (lengthSquared < 1e-20) {
      throw new Error("K 线没有足够的价格变化");
    }
    return normalized;
  }

  function buildIndicatorFeatures(contextBars, start, end) {
    if (
      start < 0
      || end < start
      || end >= contextBars.length
    ) {
      throw new Error("指标区间无效");
    }
    const selectedBars = contextBars.slice(start, end + 1);
    if (selectedBars.length < 2) {
      throw new Error("至少需要两根 K 线");
    }
    const indicators = calculateIndicators(contextBars).slice(
      start,
      end + 1,
    );
    const movingAverages = [];
    const volumeValues = [];
    const macdValues = [];
    const referenceClose = selectedBars[0].close;

    for (let index = 0; index < selectedBars.length; index += 1) {
      const bar = selectedBars[index];
      const point = indicators[index];
      for (const average of [point.ma5, point.ma10, point.ma20]) {
        movingAverages.push(
          Math.log((average === null ? bar.close : average) / referenceClose),
        );
      }
      if (!Number.isFinite(bar.volume) || bar.volume < 0) {
        throw new Error("K 线包含无效成交量");
      }
      volumeValues.push(Math.log1p(bar.volume));
      macdValues.push(
        point.dif / bar.close,
        point.dea / bar.close,
        point.macd / bar.close,
      );
    }

    return {
      kline: normalizeKline(selectedBars),
      movingAverage: standardize(movingAverages),
      volume: standardize(volumeValues),
      macd: standardize(macdValues),
    };
  }

  function featureScore(target, candidate) {
    let targetLengthSquared = 0;
    let candidateLengthSquared = 0;
    let dotProduct = 0;
    for (let index = 0; index < target.length; index += 1) {
      targetLengthSquared += target[index] * target[index];
      candidateLengthSquared += candidate[index] * candidate[index];
      dotProduct += target[index] * candidate[index];
    }

    const targetLength = Math.sqrt(targetLengthSquared);
    const candidateLength = Math.sqrt(candidateLengthSquared);
    let correlation;
    if (targetLength < 1e-12 && candidateLength < 1e-12) {
      correlation = 1;
    } else if (targetLength < 1e-12 || candidateLength < 1e-12) {
      correlation = 0;
    } else {
      correlation = dotProduct / (targetLength * candidateLength);
    }
    correlation = Math.min(1, Math.max(-1, correlation));
    return (correlation + 1) * 50;
  }

  function normalizeSimilarityFilters(selectedFilters) {
    const normalized = [];
    for (const filter of selectedFilters || []) {
      if (
        Object.prototype.hasOwnProperty.call(
          SIMILARITY_FILTER_LABELS,
          filter,
        )
        && normalized.indexOf(filter) < 0
      ) {
        normalized.push(filter);
      }
    }
    if (!normalized.length) {
      throw new Error("至少选择一项相似度指标");
    }
    return normalized;
  }

  function compositeSimilarityScore(
    scores,
    selectedFilters = DEFAULT_SIMILARITY_FILTERS,
  ) {
    const filters = normalizeSimilarityFilters(selectedFilters);
    return filters.reduce(
      (total, filter) => total + scores[filter],
      0,
    ) / filters.length;
  }

  function compareFeatures(
    target,
    candidate,
    selectedFilters = DEFAULT_SIMILARITY_FILTERS,
  ) {
    const kline = featureScore(target.kline, candidate.kline);
    const movingAverage = featureScore(
      target.movingAverage,
      candidate.movingAverage,
    );
    const volume = featureScore(target.volume, candidate.volume);
    const macd = featureScore(target.macd, candidate.macd);
    const scores = {
      kline,
      movingAverage,
      volume,
      macd,
    };
    return {
      ...scores,
      score: compositeSimilarityScore(scores, selectedFilters),
    };
  }

  function setMessage(text, type = "") {
    elements["query-message"].textContent = text;
    elements["query-message"].className = `message${type ? ` ${type}` : ""}`;
  }

  function extractCode(query) {
    const match = String(query).match(/(?:^|\D)(\d{6})(?!\d)/);
    return match ? match[1] : "";
  }

  function formatPrice(value) {
    return Number(value).toFixed(2);
  }

  function formatPercent(value) {
    return `${Number(value).toFixed(2)}%`;
  }

  function formatVolume(value) {
    if (value >= 100000000) {
      return `${(value / 100000000).toFixed(2)} 亿`;
    }
    if (value >= 10000) {
      return `${(value / 10000).toFixed(1)} 万`;
    }
    return Math.round(value).toLocaleString("zh-CN");
  }

  function canvasLayout(width, height) {
    const left = width < 520 ? 45 : 58;
    const right = width - 10;
    const top = 28;
    const bottom = height - 30;
    const gap = width < 520 ? 14 : 18;
    const contentHeight = bottom - top - gap * 2;
    const priceHeight = contentHeight * 0.57;
    const volumeHeight = contentHeight * 0.16;
    const priceBottom = top + priceHeight;
    const volumeTop = priceBottom + gap;
    const volumeBottom = volumeTop + volumeHeight;
    const macdTop = volumeBottom + gap;
    return {
      left,
      right,
      top,
      priceBottom,
      volumeTop,
      volumeBottom,
      macdTop,
      bottom,
    };
  }

  function prepareCanvas(canvas) {
    const rectangle = canvas.getBoundingClientRect();
    const width = Math.max(320, rectangle.width);
    const height = Math.max(360, rectangle.height);
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2.5);
    const outputWidth = Math.round(width * pixelRatio);
    const outputHeight = Math.round(height * pixelRatio);
    if (canvas.width !== outputWidth || canvas.height !== outputHeight) {
      canvas.width = outputWidth;
      canvas.height = outputHeight;
    }
    const context = canvas.getContext("2d");
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, width, height);
    return { context, width, height };
  }

  function drawSeries(
    context,
    values,
    horizontal,
    vertical,
    color,
    lineWidth,
  ) {
    context.beginPath();
    context.strokeStyle = color;
    context.lineWidth = lineWidth;
    let drawing = false;
    for (let index = 0; index < values.length; index += 1) {
      const value = values[index];
      if (value === null || !Number.isFinite(value)) {
        drawing = false;
        continue;
      }
      const x = horizontal(index);
      const y = vertical(value);
      if (!drawing) {
        context.moveTo(x, y);
        drawing = true;
      } else {
        context.lineTo(x, y);
      }
    }
    context.stroke();
  }

  function drawStockChart(
    canvas,
    bars,
    indicators,
    viewStart,
    viewEnd,
    selection,
  ) {
    const { context, width, height } = prepareCanvas(canvas);
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, width, height);
    if (!bars.length || viewEnd <= viewStart) {
      return;
    }

    const bounds = canvasLayout(width, height);
    const visibleBars = bars.slice(viewStart, viewEnd);
    const visibleIndicators = indicators.slice(viewStart, viewEnd);
    const count = visibleBars.length;
    const plotWidth = bounds.right - bounds.left;
    const step = plotWidth / count;
    const candleWidth = Math.max(1.2, Math.min(7, step * 0.62));

    // 使用灰色间隔带明确分隔价格、成交量和 MACD。
    context.fillStyle = "#eef1f5";
    context.fillRect(
      bounds.left,
      bounds.priceBottom,
      plotWidth,
      bounds.volumeTop - bounds.priceBottom,
    );
    context.fillRect(
      bounds.left,
      bounds.volumeBottom,
      plotWidth,
      bounds.macdTop - bounds.volumeBottom,
    );
    context.strokeStyle = "#aeb8c5";
    context.lineWidth = 1;
    for (const [panelTop, panelBottom] of [
      [bounds.top, bounds.priceBottom],
      [bounds.volumeTop, bounds.volumeBottom],
      [bounds.macdTop, bounds.bottom],
    ]) {
      context.strokeRect(
        bounds.left,
        panelTop,
        plotWidth,
        panelBottom - panelTop,
      );
    }

    const movingAverageValues = [];
    for (const point of visibleIndicators) {
      for (const value of [
        point.ma5,
        point.ma10,
        point.ma20,
        point.ma30,
        point.ma60,
        point.ma120,
      ]) {
        if (value !== null) {
          movingAverageValues.push(value);
        }
      }
    }
    let lowPrice = Math.min(
      ...visibleBars.map((bar) => bar.low),
      ...movingAverageValues,
    );
    let highPrice = Math.max(
      ...visibleBars.map((bar) => bar.high),
      ...movingAverageValues,
    );
    let priceSpan = highPrice - lowPrice;
    if (priceSpan <= 0) {
      priceSpan = Math.max(highPrice * 0.01, 0.01);
    }
    const padding = priceSpan * 0.06;
    lowPrice -= padding;
    highPrice += padding;
    priceSpan = highPrice - lowPrice;
    const priceHeight = bounds.priceBottom - bounds.top;
    const priceY = (price) => (
      bounds.top + (highPrice - price) / priceSpan * priceHeight
    );
    const candleX = (index) => bounds.left + (index + 0.5) * step;

    context.font = "10px system-ui, sans-serif";
    context.textAlign = "right";
    context.textBaseline = "middle";
    for (let line = 0; line < 4; line += 1) {
      const ratio = line / 3;
      const y = bounds.top + ratio * priceHeight;
      const price = highPrice - ratio * priceSpan;
      context.strokeStyle = "#e7e9ed";
      context.beginPath();
      context.moveTo(bounds.left, y);
      context.lineTo(bounds.right, y);
      context.stroke();
      context.fillStyle = "#59616c";
      context.fillText(price.toFixed(2), bounds.left - 5, y);
    }

    for (let index = 0; index < count; index += 1) {
      const bar = visibleBars[index];
      const x = candleX(index);
      const color = bar.close >= bar.open ? "#d84a4a" : "#239064";
      context.strokeStyle = color;
      context.fillStyle = color;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(x, priceY(bar.high));
      context.lineTo(x, priceY(bar.low));
      context.stroke();
      const bodyTop = priceY(Math.max(bar.open, bar.close));
      const bodyBottom = Math.max(
        bodyTop + 1,
        priceY(Math.min(bar.open, bar.close)),
      );
      context.fillRect(
        x - candleWidth / 2,
        bodyTop,
        candleWidth,
        bodyBottom - bodyTop,
      );
    }

    const series = [
      ["ma5", "#d39b00"],
      ["ma10", "#4e79c7"],
      ["ma20", "#9552aa"],
      ["ma30", "#df7d18"],
      ["ma60", "#238768"],
      ["ma120", "#675789"],
    ];
    for (const [name, color] of series) {
      drawSeries(
        context,
        visibleIndicators.map((point) => point[name]),
        candleX,
        priceY,
        color,
        1.5,
      );
    }

    const maxVolume = Math.max(
      1,
      ...visibleBars.map((bar) => bar.volume),
    );
    const volumeHeight = bounds.volumeBottom - bounds.volumeTop;
    for (let index = 0; index < count; index += 1) {
      const bar = visibleBars[index];
      const x = candleX(index);
      const top = (
        bounds.volumeBottom - bar.volume / maxVolume * volumeHeight
      );
      context.fillStyle = bar.close >= bar.open ? "#d84a4a" : "#239064";
      context.fillRect(
        x - candleWidth / 2,
        top,
        candleWidth,
        bounds.volumeBottom - top,
      );
    }
    context.fillStyle = "#59616c";
    context.textAlign = "left";
    context.textBaseline = "top";
    context.fillText(
      `成交量 最大 ${formatVolume(maxVolume)}`,
      bounds.left + 4,
      bounds.volumeTop + 3,
    );

    const macdValues = [];
    for (const point of visibleIndicators) {
      macdValues.push(point.dif, point.dea, point.macd);
    }
    let macdLow = Math.min(0, ...macdValues);
    let macdHigh = Math.max(0, ...macdValues);
    let macdSpan = macdHigh - macdLow;
    if (macdSpan < 1e-12) {
      macdLow = -0.5;
      macdHigh = 0.5;
      macdSpan = 1;
    }
    macdLow -= macdSpan * 0.08;
    macdHigh += macdSpan * 0.08;
    macdSpan = macdHigh - macdLow;
    const macdHeight = bounds.bottom - bounds.macdTop;
    const macdY = (value) => {
      const output = (
        bounds.macdTop
        + (macdHigh - value) / macdSpan * macdHeight
      );
      return Math.min(bounds.bottom, Math.max(bounds.macdTop, output));
    };
    const zeroY = macdY(0);
    context.strokeStyle = "#b8bdc5";
    context.beginPath();
    context.moveTo(bounds.left, zeroY);
    context.lineTo(bounds.right, zeroY);
    context.stroke();

    for (let index = 0; index < count; index += 1) {
      const point = visibleIndicators[index];
      const x = candleX(index);
      const y = macdY(point.macd);
      context.fillStyle = point.macd >= 0 ? "#d84a4a" : "#239064";
      context.fillRect(
        x - candleWidth / 2,
        Math.min(y, zeroY),
        candleWidth,
        Math.max(1, Math.abs(y - zeroY)),
      );
    }
    drawSeries(
      context,
      visibleIndicators.map((point) => point.dif),
      candleX,
      macdY,
      "#d39b00",
      1.5,
    );
    drawSeries(
      context,
      visibleIndicators.map((point) => point.dea),
      candleX,
      macdY,
      "#4e79c7",
      1.5,
    );
    context.fillStyle = "#59616c";
    context.textAlign = "left";
    context.textBaseline = "top";
    context.fillText(
      "MACD(12,26,9)  DIF  DEA",
      bounds.left + 4,
      bounds.macdTop + 3,
    );

    const labelCount = width < 520 ? 5 : 7;
    context.fillStyle = "#59616c";
    context.textAlign = "center";
    context.textBaseline = "bottom";
    for (let label = 0; label < labelCount; label += 1) {
      const index = Math.round(label * (count - 1) / (labelCount - 1));
      const date = visibleBars[index].date;
      context.fillText(
        width < 520 ? date.slice(5) : date,
        candleX(index),
        height - 5,
      );
    }

    if (selection) {
      const selectedStart = Math.max(selection[0], viewStart);
      const selectedEnd = Math.min(selection[1], viewEnd - 1);
      if (selectedStart <= selectedEnd) {
        const startX = bounds.left + (selectedStart - viewStart) * step;
        const endX = (
          bounds.left + (selectedEnd - viewStart + 1) * step
        );
        context.fillStyle = "rgb(37 99 235 / 16%)";
        context.fillRect(
          startX,
          bounds.top,
          endX - startX,
          bounds.bottom - bounds.top,
        );
        context.strokeStyle = "#2563eb";
        context.lineWidth = 2;
        context.strokeRect(
          startX,
          bounds.top,
          endX - startX,
          bounds.bottom - bounds.top,
        );
      }
    }
  }

  function drawMainChart() {
    drawStockChart(
      elements["main-chart"],
      state.targetBars,
      state.targetIndicators,
      state.viewStart,
      state.viewEnd,
      state.selection,
    );
  }

  function drawDetailChart() {
    if (!state.detailBars.length) {
      return;
    }
    drawStockChart(
      elements["detail-chart"],
      state.detailBars,
      state.detailIndicators,
      state.detailStart,
      state.detailEnd,
      null,
    );
  }

  function drawFavoritePreviewChart() {
    if (!state.favoritePreviewBars.length) {
      return;
    }
    drawStockChart(
      elements["favorite-preview-chart"],
      state.favoritePreviewBars,
      state.favoritePreviewIndicators,
      0,
      state.favoritePreviewBars.length,
      state.favoritePreviewSelection,
    );
  }

  function updateViewControls() {
    if (!state.targetBars.length) {
      return;
    }
    const count = state.targetBars.length;
    elements["view-start"].max = String(count - 1);
    elements["view-end"].max = String(count - 1);
    elements["view-start"].value = String(state.viewStart);
    elements["view-end"].value = String(state.viewEnd - 1);
    elements["view-label"].textContent = (
      `${state.targetBars[state.viewStart].date} 至 `
      + `${state.targetBars[state.viewEnd - 1].date}，`
      + `${state.viewEnd - state.viewStart} 根`
    );
  }

  function clearSelection() {
    state.selection = null;
    state.dragAnchor = null;
    elements["selection-summary"].textContent = (
      `请在图表上拖动选择 ${MIN_SELECTION}～${MAX_SELECTION} 根连续 K 线`
    );
    elements["zoom-selection"].disabled = true;
    elements["save-favorite"].disabled = true;
    elements["search-button"].disabled = true;
    drawMainChart();
  }

  function setView(start, end, keepSelection = false) {
    const count = state.targetBars.length;
    if (!count) {
      return;
    }
    let normalizedStart = Math.max(0, Math.min(start, count - 1));
    let normalizedEnd = Math.max(
      normalizedStart + 1,
      Math.min(end, count),
    );
    if (normalizedEnd - normalizedStart < MIN_SELECTION) {
      normalizedEnd = Math.min(count, normalizedStart + MIN_SELECTION);
      normalizedStart = Math.max(0, normalizedEnd - MIN_SELECTION);
    }
    state.viewStart = normalizedStart;
    state.viewEnd = normalizedEnd;
    updateViewControls();
    if (!keepSelection) {
      clearSelection();
    } else {
      drawMainChart();
    }
  }

  function updateSelection(start, end) {
    if (!state.targetBars.length) {
      return;
    }
    const normalizedStart = Math.max(0, Math.min(start, end));
    const normalizedEnd = Math.min(
      state.targetBars.length - 1,
      Math.max(start, end),
    );
    state.selection = [normalizedStart, normalizedEnd];
    const count = normalizedEnd - normalizedStart + 1;
    elements["selection-summary"].textContent = (
      `${state.targetBars[normalizedStart].date} 至 `
      + `${state.targetBars[normalizedEnd].date}，共 ${count} 根 K 线`
    );
    const valid = count >= MIN_SELECTION && count <= MAX_SELECTION;
    elements["zoom-selection"].disabled = !valid;
    elements["save-favorite"].disabled = !valid;
    elements["search-button"].disabled = !valid;
    drawMainChart();
  }

  function eventClientPoint(event) {
    const touch = (
      event.touches && event.touches[0]
    ) || (
      event.changedTouches && event.changedTouches[0]
    );
    const x = touch ? touch.clientX : event.clientX;
    const y = touch ? touch.clientY : event.clientY;
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      return null;
    }
    return {
      x,
      y,
      isTouch: Boolean(
        touch
        || event.pointerType === "touch"
        || event.pointerType === "pen"
      ),
    };
  }

  // 斜向移动暂不判定，等待后续位移形成明确的横向或纵向意图。
  function gestureDirection(gesture, point) {
    const distanceX = Math.abs(point.x - gesture.startX);
    const distanceY = Math.abs(point.y - gesture.startY);
    if (
      distanceX < GESTURE_MOVE_THRESHOLD
      && distanceY < GESTURE_MOVE_THRESHOLD
    ) {
      return null;
    }
    if (distanceY > distanceX * GESTURE_DIRECTION_RATIO) {
      return "vertical";
    }
    if (distanceX > distanceY * GESTURE_DIRECTION_RATIO) {
      return "horizontal";
    }
    return null;
  }

  function chartPointerIndex(clientX) {
    if (!state.targetBars.length) {
      return null;
    }
    if (!Number.isFinite(clientX)) {
      return null;
    }
    const rectangle = elements["main-chart"].getBoundingClientRect();
    const layout = canvasLayout(rectangle.width, rectangle.height);
    const x = clientX - rectangle.left;
    const ratio = (
      (x - layout.left) / (layout.right - layout.left)
    );
    const localIndex = Math.floor(
      Math.min(0.999999, Math.max(0, ratio))
      * (state.viewEnd - state.viewStart),
    );
    return state.viewStart + localIndex;
  }

  function startChartSelection(event, index) {
    state.dragAnchor = index;
    updateSelection(index, index);
    if (
      event.pointerId !== undefined
      && typeof elements["main-chart"].setPointerCapture === "function"
    ) {
      try {
        elements["main-chart"].setPointerCapture(event.pointerId);
      } catch (error) {
        // 部分安卓 WebView 声明接口但不允许捕获，拖动仍可继续。
      }
    }
    if (event.cancelable) {
      event.preventDefault();
    }
  }

  function handlePointerDown(event) {
    if (event.button !== undefined && event.button !== 0) {
      return;
    }
    if (event.touches && event.touches.length !== 1) {
      state.chartGesture = null;
      state.dragAnchor = null;
      return;
    }
    const point = eventClientPoint(event);
    const index = point ? chartPointerIndex(point.x) : null;
    if (index === null) {
      return;
    }
    state.chartGesture = {
      startX: point.x,
      startY: point.y,
      startIndex: index,
      active: !point.isTouch,
      previousSelection: state.selection ? [...state.selection] : null,
    };
    if (state.chartGesture.active) {
      startChartSelection(event, index);
    }
  }

  function handlePointerMove(event) {
    const gesture = state.chartGesture;
    if (!gesture) {
      return;
    }
    const point = eventClientPoint(event);
    if (!point) {
      return;
    }
    if (!gesture.active) {
      const direction = gestureDirection(gesture, point);
      if (direction === "vertical") {
        state.chartGesture = null;
        state.dragAnchor = null;
        return;
      }
      if (direction !== "horizontal") {
        return;
      }
      gesture.active = true;
      startChartSelection(event, gesture.startIndex);
    }
    let index = chartPointerIndex(point.x);
    if (index === null) {
      return;
    }
    if (index > state.dragAnchor + MAX_SELECTION - 1) {
      index = state.dragAnchor + MAX_SELECTION - 1;
    } else if (index < state.dragAnchor - MAX_SELECTION + 1) {
      index = state.dragAnchor - MAX_SELECTION + 1;
    }
    updateSelection(state.dragAnchor, index);
    if (event.cancelable) {
      event.preventDefault();
    }
  }

  function handlePointerUp(event) {
    const gesture = state.chartGesture;
    if (!gesture) {
      return;
    }
    handlePointerMove(event);
    if (!gesture.active || state.dragAnchor === null || !state.selection) {
      state.chartGesture = null;
      state.dragAnchor = null;
      return;
    }
    let [start, end] = state.selection;
    if (end - start + 1 < MIN_SELECTION) {
      end = Math.min(
        state.targetBars.length - 1,
        start + MIN_SELECTION - 1,
      );
      start = Math.max(0, end - MIN_SELECTION + 1);
      updateSelection(start, end);
    }
    state.chartGesture = null;
    state.dragAnchor = null;
    if (event.cancelable) {
      event.preventDefault();
    }
  }

  function handlePointerCancel() {
    const gesture = state.chartGesture;
    state.chartGesture = null;
    state.dragAnchor = null;
    if (!gesture || !gesture.active) {
      return;
    }
    if (gesture.previousSelection) {
      updateSelection(
        gesture.previousSelection[0],
        gesture.previousSelection[1],
      );
    } else {
      clearSelection();
    }
  }

  function bindChartSelectionEvents() {
    if ("PointerEvent" in window) {
      elements["main-chart"].addEventListener(
        "pointerdown",
        handlePointerDown,
      );
      elements["main-chart"].addEventListener(
        "pointermove",
        handlePointerMove,
      );
      elements["main-chart"].addEventListener(
        "pointerup",
        handlePointerUp,
      );
      elements["main-chart"].addEventListener(
        "pointercancel",
        handlePointerCancel,
      );
      return;
    }

    // 兼容较旧荣耀浏览器和安卓 WebView 的触摸、鼠标事件。
    elements["main-chart"].addEventListener(
      "touchstart",
      handlePointerDown,
      { passive: false },
    );
    elements["main-chart"].addEventListener(
      "touchmove",
      handlePointerMove,
      { passive: false },
    );
    elements["main-chart"].addEventListener(
      "touchend",
      handlePointerUp,
      { passive: false },
    );
    elements["main-chart"].addEventListener(
      "touchcancel",
      handlePointerCancel,
      { passive: true },
    );
    elements["main-chart"].addEventListener(
      "mousedown",
      handlePointerDown,
    );
    window.addEventListener("mousemove", handlePointerMove);
    window.addEventListener("mouseup", handlePointerUp);
  }

  // 时间轴纵向滑动时恢复起始值（防误触），
  // 横向拖动后手指上抬越高，滑块移动越精细（灵敏度越低）。
  function bindScrollSafeRange(element, updateView) {
    let gesture = null;

    function restoreValue() {
      if (!gesture || element.value === String(gesture.startValue)) {
        return;
      }
      element.value = String(gesture.startValue);
      updateView();
    }

    function applyPrecisionValue() {
      if (!gesture || !gesture.precision) {
        return;
      }
      const rect = element.getBoundingClientRect();
      if (rect.width <= 0) {
        return;
      }
      const min = Number(element.min);
      const max = Number(element.max);
      const range = max - min;
      if (range <= 0) {
        return;
      }
      const ratio = Math.max(
        0,
        Math.min(1, (gesture.currentX - rect.left) / rect.width),
      );
      const nativeValue = min + ratio * range;
      const nativeDelta = nativeValue - gesture.startValue;
      const scaledDelta = nativeDelta * gesture.sensitivity;
      const desiredValue = gesture.startValue + scaledDelta;
      const clamped = Math.max(min, Math.min(max, desiredValue));
      element.value = String(Math.round(clamped));
      updateView();
    }

    function begin(event) {
      const point = eventClientPoint(event);
      if (!point || !point.isTouch) {
        gesture = null;
        return;
      }
      gesture = {
        startX: point.x,
        startY: point.y,
        startValue: Number(element.value),
        vertical: false,
        precision: false,
        sensitivity: 1,
        currentX: point.x,
      };
    }

    function move(event) {
      if (!gesture || gesture.vertical) {
        return;
      }
      const point = eventClientPoint(event);
      if (!point) {
        return;
      }
      const direction = gestureDirection(gesture, point);
      if (direction === "vertical") {
        gesture.vertical = true;
        restoreValue();
        return;
      }
      if (direction === "horizontal") {
        gesture.precision = true;
      }
      if (gesture.precision) {
        gesture.currentX = point.x;
        // 手指上抬越高（Y 值越小），灵敏度越低，横向移动幅度越小。
        const lift = Math.max(0, gesture.startY - point.y);
        gesture.sensitivity = 1 / (1 + lift / 50);
        applyPrecisionValue();
      }
    }

    function finish(cancelled = false) {
      if (gesture && (gesture.vertical || cancelled)) {
        restoreValue();
      }
      gesture = null;
    }

    element.addEventListener("input", () => {
      if (gesture && gesture.vertical) {
        restoreValue();
        return;
      }
      if (gesture && gesture.precision) {
        applyPrecisionValue();
        return;
      }
      updateView();
    });

    if ("PointerEvent" in window) {
      element.addEventListener("pointerdown", begin);
      element.addEventListener("pointermove", move);
      element.addEventListener("pointerup", () => finish());
      element.addEventListener("pointercancel", () => finish(true));
      return;
    }

    element.addEventListener("touchstart", begin, { passive: true });
    element.addEventListener("touchmove", move, { passive: true });
    element.addEventListener("touchend", () => finish(), { passive: true });
    element.addEventListener(
      "touchcancel",
      () => finish(true),
      { passive: true },
    );
  }

  // 结果项发生滚动位移后屏蔽浏览器补发的合成点击。
  function bindScrollSafeClick(element, action) {
    let gesture = null;
    let blockClickUntil = 0;

    function begin(event) {
      const point = eventClientPoint(event);
      if (!point || !point.isTouch) {
        gesture = null;
        return;
      }
      gesture = {
        startX: point.x,
        startY: point.y,
        moved: false,
      };
    }

    function move(event) {
      if (!gesture || gesture.moved) {
        return;
      }
      const point = eventClientPoint(event);
      if (!point) {
        return;
      }
      const distanceX = Math.abs(point.x - gesture.startX);
      const distanceY = Math.abs(point.y - gesture.startY);
      if (
        distanceX >= GESTURE_MOVE_THRESHOLD
        || distanceY >= GESTURE_MOVE_THRESHOLD
      ) {
        gesture.moved = true;
      }
    }

    function finish(event, cancelled = false) {
      move(event);
      if (cancelled || (gesture && gesture.moved)) {
        blockClickUntil = Date.now() + SCROLL_CLICK_BLOCK_MS;
      }
      gesture = null;
    }

    if ("PointerEvent" in window) {
      element.addEventListener("pointerdown", begin);
      element.addEventListener("pointermove", move);
      element.addEventListener("pointerup", (event) => finish(event));
      element.addEventListener(
        "pointercancel",
        (event) => finish(event, true),
      );
    } else {
      element.addEventListener("touchstart", begin, { passive: true });
      element.addEventListener("touchmove", move, { passive: true });
      element.addEventListener(
        "touchend",
        (event) => finish(event),
        { passive: true },
      );
      element.addEventListener(
        "touchcancel",
        (event) => finish(event, true),
        { passive: true },
      );
    }

    element.addEventListener("click", (event) => {
      if (Date.now() < blockClickUntil) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      action();
    });
  }

  function loadTarget(query) {
    const code = extractCode(query);
    if (!code) {
      setMessage("请输入有效的六位 A 股代码", "error");
      return;
    }
    const stock = state.stockByCode.get(code);
    if (!stock) {
      setMessage(`行情数据中没有找到股票 ${code}`, "error");
      return;
    }
    const bars = readBars(stock);
    if (bars.length < MIN_SELECTION) {
      setMessage(`${code} 的有效 K 线不足 ${MIN_SELECTION} 根`, "error");
      return;
    }

    state.searchToken += 1;
    state.target = stock;
    state.targetIsFavorite = false;
    state.targetBars = bars;
    state.targetIndicators = calculateIndicators(bars);
    state.selection = null;
    state.candidateResults = [];
    state.results = [];
    elements["chart-placeholder"].hidden = true;
    elements["chart-title"].textContent = `${stock.name} ${stock.code}`;
    elements["stock-summary"].textContent = (
      `${stock.exchange} · ${bars[0].date} 至 `
      + `${bars[bars.length - 1].date} · ${bars.length} 根日 K`
    );
    elements["view-start"].disabled = false;
    elements["view-end"].disabled = false;
    elements["reset-view"].disabled = false;
    elements["result-summary"].textContent = "完成片段选择后开始匹配";
    replaceElementChildren(
      elements["result-list"],
      createEmptyState("尚无匹配结果"),
    );
    setView(
      Math.max(0, bars.length - DEFAULT_VIEW_BARS),
      bars.length,
      true,
    );
    clearSelection();
    setMessage(
      `已加载 ${stock.name}（${stock.code}.${stock.exchange}）`,
      "success",
    );
  }

  function createEmptyState(text) {
    const element = document.createElement("div");
    element.className = "empty-state";
    element.textContent = text;
    return element;
  }

  function updateFavoritesButton() {
    const count = state.favorites.length;
    elements["favorites-button"].textContent = (
      count ? `收藏夹 (${count})` : "收藏夹"
    );
  }

  function formatFavoriteCreatedAt(value) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) {
      return value.slice(0, 19).replace("T", " ");
    }
    return date.toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function showFavoritePreview(favorite) {
    state.selectedFavoriteId = favorite ? favorite.id : null;
    state.favoritePreviewBars = favorite
      ? favorite.contextBars.map((bar) => ({ ...bar }))
      : [];
    state.favoritePreviewIndicators = state.favoritePreviewBars.length
      ? calculateIndicators(state.favoritePreviewBars)
      : [];
    state.favoritePreviewSelection = favorite
      ? [
        favorite.selectionStart,
        favorite.selectionStart + favorite.selectionCount - 1,
      ]
      : null;
    elements["favorite-search"].disabled = !favorite;
    elements["favorite-delete"].disabled = !favorite;
    elements["favorite-preview-placeholder"].hidden = Boolean(favorite);
    elements["favorite-preview-summary"].textContent = favorite
      ? (
        `${favorite.stock.name}（${favorite.stock.code}） · `
        + `${favorite.contextBars[favorite.selectionStart].date} 至 `
        + `${favorite.contextBars[
          favorite.selectionStart + favorite.selectionCount - 1
        ].date} · ${favorite.selectionCount} 根`
      )
      : "选择左侧收藏以预览 K 线走势";
    if (favorite) {
      window.requestAnimationFrame(drawFavoritePreviewChart);
    }
  }

  function selectFavoriteById(favoriteId) {
    const favorite = state.favorites.find(
      (item) => item.id === favoriteId,
    ) || null;
    showFavoritePreview(favorite);
    for (const item of elements["favorites-list"].querySelectorAll(
      ".favorite-item",
    )) {
      item.classList.toggle(
        "selected",
        Number(item.dataset.favoriteId) === favoriteId,
      );
    }
    return favorite;
  }

  function renderFavorites() {
    replaceElementChildren(elements["favorites-list"]);
    updateFavoritesButton();
    if (!state.favorites.length) {
      elements["favorites-summary"].textContent = state.favoriteStorageError
        ? `收藏读取失败：${state.favoriteStorageError}`
        : "收藏夹为空";
      appendElementChildren(
        elements["favorites-list"],
        createEmptyState("请先在主图框选并收藏 K 线"),
      );
      showFavoritePreview(null);
      return;
    }

    elements["favorites-summary"].textContent = (
      `共 ${state.favorites.length} 条收藏，选择后可预览或直接搜索`
    );
    for (const favorite of state.favorites) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "favorite-item";
      button.dataset.favoriteId = String(favorite.id);
      const strong = document.createElement("strong");
      strong.textContent = favorite.name;
      const description = document.createElement("span");
      description.textContent = (
        `${favorite.selectionCount} 根 K 线 · `
        + `${formatFavoriteCreatedAt(favorite.createdAt)}`
      );
      appendElementChildren(button, strong, description);
      bindScrollSafeClick(button, () => {
        selectFavoriteById(favorite.id);
      });
      elements["favorites-list"].appendChild(button);
    }

    const selected = state.favorites.some(
      (favorite) => favorite.id === state.selectedFavoriteId,
    )
      ? state.selectedFavoriteId
      : state.favorites[0].id;
    selectFavoriteById(selected);
  }

  function openFavorites() {
    loadFavorites();
    renderFavorites();
    elements["favorites-modal"].hidden = false;
    document.body.classList.add("modal-open");
    window.requestAnimationFrame(drawFavoritePreviewChart);
  }

  function closeFavorites() {
    elements["favorites-modal"].hidden = true;
    if (elements["detail-modal"].hidden) {
      document.body.classList.remove("modal-open");
    }
  }

  function saveCurrentFavorite() {
    if (!state.target || !state.selection) {
      return null;
    }
    const [selectionStart, selectionEnd] = state.selection;
    const selectionCount = selectionEnd - selectionStart + 1;
    if (
      selectionCount < MIN_SELECTION
      || selectionCount > MAX_SELECTION
    ) {
      setMessage(
        `请选择 ${MIN_SELECTION}～${MAX_SELECTION} 根连续 K 线`,
        "error",
      );
      return null;
    }
    const contextStart = Math.max(
      0,
      selectionStart - FAVORITE_PREVIEW_WARMUP,
    );
    const contextBars = state.targetBars
      .slice(contextStart, selectionEnd + 1)
      .map((bar) => ({ ...bar }));
    const selectedStart = selectionStart - contextStart;
    const firstBar = contextBars[selectedStart];
    const lastBar = contextBars[selectedStart + selectionCount - 1];
    const favorite = normalizeFavorite({
      id: nextFavoriteId(),
      name: `${state.target.name} ${firstBar.date} 至 ${lastBar.date}`,
      createdAt: new Date().toISOString(),
      stock: {
        market: state.target.market,
        code: state.target.code,
        name: state.target.name,
        exchange: state.target.exchange,
      },
      contextBars,
      selectionStart: selectedStart,
      selectionCount,
    });
    if (!favorite) {
      setMessage("收藏数据校验失败", "error");
      return null;
    }
    try {
      persistFavorites([favorite, ...state.favorites]);
    } catch (error) {
      setMessage(`收藏失败：${error.message || String(error)}`, "error");
      return null;
    }
    updateFavoritesButton();
    setMessage(
      `已收藏 ${favorite.stock.name} 的 ${selectionCount} 根 K 线`,
      "success",
    );
    return favorite;
  }

  function deleteFavorite(favorite, requireConfirmation = true) {
    if (!favorite) {
      return false;
    }
    if (
      requireConfirmation
      && !window.confirm(`确定删除“${favorite.name}”吗？`)
    ) {
      return false;
    }
    try {
      persistFavorites(
        state.favorites.filter((item) => item.id !== favorite.id),
      );
    } catch (error) {
      setMessage(`删除收藏失败：${error.message || String(error)}`, "error");
      return false;
    }
    state.selectedFavoriteId = null;
    renderFavorites();
    setMessage("收藏已删除", "success");
    return true;
  }

  function restoreFavoriteTarget(favorite) {
    const stock = state.stockByCode.get(favorite.stock.code)
      || { ...favorite.stock };
    const bars = favorite.contextBars.map((bar) => ({ ...bar }));
    const selectionEnd = (
      favorite.selectionStart + favorite.selectionCount - 1
    );
    state.searchToken += 1;
    state.target = stock;
    state.targetIsFavorite = true;
    state.targetBars = bars;
    state.targetIndicators = calculateIndicators(bars);
    state.selection = null;
    state.candidateResults = [];
    state.results = [];
    elements["stock-query"].value = stock.code;
    elements["chart-placeholder"].hidden = true;
    elements["chart-title"].textContent = `${stock.name} ${stock.code}`;
    elements["stock-summary"].textContent = (
      `收藏形态 · ${bars[favorite.selectionStart].date} 至 `
      + `${bars[selectionEnd].date} · ${favorite.selectionCount} 根日 K`
    );
    elements["view-start"].disabled = false;
    elements["view-end"].disabled = false;
    elements["reset-view"].disabled = false;
    elements["result-summary"].textContent = "正在从收藏准备相似匹配";
    replaceElementChildren(
      elements["result-list"],
      createEmptyState("正在准备收藏形态"),
    );
    setView(0, bars.length, true);
    updateSelection(favorite.selectionStart, selectionEnd);
    setView(
      Math.max(0, favorite.selectionStart - 2),
      Math.min(bars.length, selectionEnd + 3),
      true,
    );
  }

  async function searchSelectedFavorite() {
    const favorite = selectedFavorite();
    if (!favorite) {
      return;
    }
    closeFavorites();
    restoreFavoriteTarget(favorite);
    setMessage(`正在搜索收藏“${favorite.name}”的相似走势`);
    await searchSimilar();
  }

  function nextTask() {
    return new Promise((resolve) => {
      window.setTimeout(resolve, 0);
    });
  }

  function updateProgress(completed, total) {
    const ratio = total ? completed / total : 0;
    elements["progress-bar"].style.width = `${ratio * 100}%`;
    elements["progress-label"].textContent = (
      `正在计算 ${completed.toLocaleString("zh-CN")} / `
      + `${total.toLocaleString("zh-CN")}`
    );
  }

  function getSelectedSimilarityFilters() {
    return DEFAULT_SIMILARITY_FILTERS.filter(
      (filter) => elements[`filter-${filter}`].checked,
    );
  }

  function formatSimilarityFilters(filters) {
    return filters.map(
      (filter) => SIMILARITY_FILTER_LABELS[filter],
    ).join("、");
  }

  function similarityFilterKey(filters) {
    return normalizeSimilarityFilters(filters).join(",");
  }

  function compareSimilarityResults(first, second, filters) {
    const firstScore = compositeSimilarityScore(first, filters);
    const secondScore = compositeSimilarityScore(second, filters);
    return (
      secondScore - firstScore
      || first.stock.code.localeCompare(second.stock.code)
    );
  }

  function retainTopSimilarityResult(buckets, result) {
    for (const filters of SIMILARITY_FILTER_COMBINATIONS) {
      const key = similarityFilterKey(filters);
      const bucket = buckets.get(key);
      bucket.push(result);
      bucket.sort((first, second) => (
        compareSimilarityResults(first, second, filters)
      ));
      if (bucket.length > RESULT_COUNT) {
        bucket.pop();
      }
    }
  }

  function mergeTopSimilarityResults(buckets) {
    const merged = new Map();
    for (const bucket of buckets.values()) {
      for (const result of bucket) {
        merged.set(`${result.stock.market}:${result.stock.code}`, result);
      }
    }
    return [...merged.values()];
  }

  function applySimilarityFilters() {
    const filters = normalizeSimilarityFilters(
      getSelectedSimilarityFilters(),
    );
    for (const result of state.candidateResults) {
      result.score = compositeSimilarityScore(result, filters);
    }
    state.candidateResults.sort((first, second) => (
      second.score - first.score
      || first.stock.code.localeCompare(second.stock.code)
    ));
    state.results = state.candidateResults.slice(0, RESULT_COUNT);
    renderResults();
    if (state.results.length) {
      elements["result-summary"].textContent = (
        `候选截止 ${state.marketDate}，按`
        + `${formatSimilarityFilters(filters)}综合相似度从高到低显示 `
        + `${state.results.length} 只股票`
      );
    }
  }

  function handleSimilarityFilterChange(event) {
    const filters = getSelectedSimilarityFilters();
    if (!filters.length) {
      event.currentTarget.checked = true;
      setMessage("至少保留一项相似度指标", "error");
      return;
    }
    if (state.candidateResults.length) {
      applySimilarityFilters();
      setMessage(
        `已按${formatSimilarityFilters(filters)}综合相似度重新排序`,
        "success",
      );
    }
  }

  async function searchSimilar() {
    if (!state.target || !state.selection) {
      return;
    }
    if (liveDataIsStale()) {
      setMessage("正在获取全市场最新行情，完成后自动开始匹配");
      try {
        await refreshLiveMarket();
      } catch (error) {
        setMessage(
          `无法更新实时行情，将使用当前数据：`
          + `${error.message || String(error)}`,
          "error",
        );
      }
    } else if (state.liveRefreshPromise) {
      try {
        await state.liveRefreshPromise;
      } catch (_error) {
        // 自动更新失败时继续使用内置行情。
      }
    }
    if (!state.target || !state.selection) {
      return;
    }
    const [selectionStart, selectionEnd] = state.selection;
    const selectedCount = selectionEnd - selectionStart + 1;
    if (
      selectedCount < MIN_SELECTION
      || selectedCount > MAX_SELECTION
    ) {
      setMessage(
        `请选择 ${MIN_SELECTION}～${MAX_SELECTION} 根连续 K 线`,
        "error",
      );
      return;
    }
    const selectedFilters = getSelectedSimilarityFilters();
    if (!selectedFilters.length) {
      setMessage("至少选择一项相似度指标", "error");
      return;
    }

    const token = state.searchToken + 1;
    state.searchToken = token;
    state.candidateResults = [];
    elements["load-button"].disabled = true;
    elements["search-button"].disabled = true;
    elements["progress-wrap"].hidden = false;
    updateProgress(0, state.stocks.length);
    elements["result-summary"].textContent = "正在搜索全市场最新走势";
    setMessage(
      `正在进行${formatSimilarityFilters(selectedFilters)}综合匹配，请稍候`,
    );

    // 搜索开始后自动放大选中的 K 线，并保留少量前后边距。
    setView(
      Math.max(0, selectionStart - 2),
      Math.min(state.targetBars.length, selectionEnd + 3),
      true,
    );

    const warmup = Math.min(INDICATOR_WARMUP, selectionStart);
    const targetContext = state.targetBars.slice(
      selectionStart - warmup,
      selectionEnd + 1,
    );
    let targetFeatures;
    try {
      targetFeatures = buildIndicatorFeatures(
        targetContext,
        warmup,
        warmup + selectedCount - 1,
      );
    } catch (error) {
      finishSearchWithError(error.message || String(error));
      return;
    }

    const contextCount = selectedCount + warmup;
    const validStocks = state.stocks.filter((stock) => (
      latestStockDate(stock) === state.marketDate
      && availableStockBarCount(stock) >= contextCount
    ));
    const topResultBuckets = new Map(
      SIMILARITY_FILTER_COMBINATIONS.map((filters) => [
        similarityFilterKey(filters),
        [],
      ]),
    );
    const batchSize = 80;
    updateProgress(0, validStocks.length);
    for (
      let batchStart = 0;
      batchStart < validStocks.length;
      batchStart += batchSize
    ) {
      if (token !== state.searchToken) {
        return;
      }
      const batchEnd = Math.min(
        validStocks.length,
        batchStart + batchSize,
      );
      for (let index = batchStart; index < batchEnd; index += 1) {
        const stock = validStocks[index];
        if (
          stock.market === state.target.market
          && stock.code === state.target.code
        ) {
          continue;
        }
        const candidateBars = readBars(stock, contextCount);
        if (
          candidateBars.length < contextCount
          || candidateBars[candidateBars.length - 1].date
            !== state.marketDate
        ) {
          continue;
        }
        try {
          const candidateFeatures = buildIndicatorFeatures(
            candidateBars,
            warmup,
            candidateBars.length - 1,
          );
          const result = {
            stock,
            ...compareFeatures(
              targetFeatures,
              candidateFeatures,
              selectedFilters,
            ),
          };
          retainTopSimilarityResult(topResultBuckets, result);
        } catch (_error) {
          // 单只股票数据异常时跳过，不中断全市场搜索。
        }
      }
      updateProgress(batchEnd, validStocks.length);
      await nextTask();
    }

    if (token !== state.searchToken) {
      return;
    }
    state.candidateResults = mergeTopSimilarityResults(topResultBuckets);
    applySimilarityFilters();
    elements["load-button"].disabled = false;
    elements["search-button"].disabled = false;
    elements["progress-wrap"].hidden = true;
    if (state.results.length) {
      setMessage("全市场匹配完成", "success");
      elements["result-list"].scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    } else {
      elements["result-summary"].textContent = "没有找到可用候选股票";
      setMessage("没有找到日期一致且数据完整的候选股票", "error");
    }
  }

  function finishSearchWithError(message) {
    elements["load-button"].disabled = false;
    elements["search-button"].disabled = false;
    elements["progress-wrap"].hidden = true;
    elements["result-summary"].textContent = "匹配失败";
    setMessage(`无法计算相似度：${message}`, "error");
  }

  function scoreCell(label, value) {
    const element = document.createElement("span");
    element.appendChild(document.createTextNode(label));
    const strong = document.createElement("strong");
    strong.textContent = formatPercent(value);
    element.appendChild(strong);
    return element;
  }

  function renderResults() {
    replaceElementChildren(elements["result-list"]);
    if (!state.results.length) {
      appendElementChildren(
        elements["result-list"],
        createEmptyState("没有找到可用的匹配结果"),
      );
      return;
    }

    state.results.forEach((result, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "result-item";
      button.setAttribute(
        "aria-label",
        `查看第 ${index + 1} 名 ${result.stock.name} 走势详情`,
      );

      const main = document.createElement("span");
      main.className = "result-main";
      const identity = document.createElement("span");
      identity.className = "result-identity";
      const rank = document.createElement("span");
      rank.className = "result-rank";
      rank.textContent = String(index + 1);
      const name = document.createElement("span");
      name.className = "result-name";
      const strong = document.createElement("strong");
      strong.textContent = result.stock.name;
      const symbol = document.createElement("span");
      symbol.textContent = (
        `${result.stock.code}.${result.stock.exchange}`
      );
      appendElementChildren(name, strong, symbol);
      appendElementChildren(identity, rank, name);
      const total = document.createElement("span");
      total.className = "result-score";
      total.textContent = formatPercent(result.score);
      appendElementChildren(main, identity, total);

      const scores = document.createElement("span");
      scores.className = "score-grid";
      appendElementChildren(
        scores,
        scoreCell("K 线", result.kline),
        scoreCell("均线", result.movingAverage),
        scoreCell("成交量", result.volume),
        scoreCell("MACD", result.macd),
      );
      appendElementChildren(button, main, scores);
      bindScrollSafeClick(button, () => {
        openDetail(result.stock);
      });
      elements["result-list"].appendChild(button);
    });
  }

  function updateDetailView() {
    if (!state.detailBars.length) {
      return;
    }
    elements["detail-start"].value = String(state.detailStart);
    elements["detail-end"].value = String(state.detailEnd - 1);
    elements["detail-view-label"].textContent = (
      `${state.detailBars[state.detailStart].date} 至 `
      + `${state.detailBars[state.detailEnd - 1].date}，`
      + `${state.detailEnd - state.detailStart} 根`
    );
    drawDetailChart();
  }

  function setDetailView(start, end) {
    const count = state.detailBars.length;
    let normalizedStart = Math.max(0, Math.min(start, count - 1));
    let normalizedEnd = Math.max(
      normalizedStart + 1,
      Math.min(end, count),
    );
    if (normalizedEnd - normalizedStart < MIN_SELECTION) {
      normalizedEnd = Math.min(
        count,
        normalizedStart + MIN_SELECTION,
      );
      normalizedStart = Math.max(
        0,
        normalizedEnd - MIN_SELECTION,
      );
    }
    state.detailStart = normalizedStart;
    state.detailEnd = normalizedEnd;
    updateDetailView();
  }

  function openDetail(stock) {
    const bars = readBars(stock);
    state.detailStock = stock;
    state.detailBars = bars;
    state.detailIndicators = calculateIndicators(bars);
    state.detailStart = Math.max(0, bars.length - DEFAULT_VIEW_BARS);
    state.detailEnd = bars.length;
    elements["detail-title"].textContent = `${stock.name} ${stock.code}`;
    elements["detail-summary"].textContent = (
      `${stock.exchange} · ${bars[0].date} 至 `
      + `${bars[bars.length - 1].date} · ${bars.length} 根日 K`
    );
    elements["detail-start"].min = "0";
    elements["detail-start"].max = String(bars.length - 1);
    elements["detail-end"].min = "0";
    elements["detail-end"].max = String(bars.length - 1);
    elements["detail-modal"].hidden = false;
    document.body.classList.add("modal-open");
    window.requestAnimationFrame(updateDetailView);
  }

  function closeDetail() {
    elements["detail-modal"].hidden = true;
    document.body.classList.remove("modal-open");
    state.detailStock = null;
    state.detailBars = [];
    state.detailIndicators = [];
  }

  function handleViewStart() {
    const start = Number(elements["view-start"].value);
    let end = state.viewEnd;
    if (end - start < MIN_SELECTION) {
      end = Math.min(state.targetBars.length, start + MIN_SELECTION);
    }
    setView(start, end);
  }

  function handleViewEnd() {
    const end = Number(elements["view-end"].value) + 1;
    let start = state.viewStart;
    if (end - start < MIN_SELECTION) {
      start = Math.max(0, end - MIN_SELECTION);
    }
    setView(start, end);
  }

  function bindEvents() {
    elements["query-form"].addEventListener("submit", (event) => {
      event.preventDefault();
      loadTarget(elements["stock-query"].value);
    });
    elements["refresh-live"].addEventListener("click", () => {
      refreshLiveMarket({ manual: true }).catch(() => {
        // 错误信息已显示在页面中。
      });
    });
    bindChartSelectionEvents();
    bindScrollSafeRange(elements["view-start"], handleViewStart);
    bindScrollSafeRange(elements["view-end"], handleViewEnd);
    elements["reset-view"].addEventListener("click", () => {
      setView(0, state.targetBars.length);
    });
    elements["zoom-selection"].addEventListener("click", () => {
      if (!state.selection) {
        return;
      }
      setView(
        Math.max(0, state.selection[0] - 2),
        Math.min(state.targetBars.length, state.selection[1] + 3),
        true,
      );
    });
    elements["save-favorite"].addEventListener("click", saveCurrentFavorite);
    elements["favorites-button"].addEventListener("click", openFavorites);
    elements["favorites-close"].addEventListener("click", closeFavorites);
    elements["favorites-modal"].addEventListener("click", (event) => {
      if (event.target.hasAttribute("data-close-favorites")) {
        closeFavorites();
      }
    });
    elements["favorite-search"].addEventListener("click", () => {
      searchSelectedFavorite().catch((error) => {
        finishSearchWithError(error.message || String(error));
      });
    });
    elements["favorite-delete"].addEventListener("click", () => {
      deleteFavorite(selectedFavorite());
    });
    elements["search-button"].addEventListener("click", searchSimilar);
    for (const filter of DEFAULT_SIMILARITY_FILTERS) {
      elements[`filter-${filter}`].addEventListener(
        "change",
        handleSimilarityFilterChange,
      );
    }
    elements["detail-close"].addEventListener("click", closeDetail);
    elements["detail-modal"].addEventListener("click", (event) => {
      if (event.target.hasAttribute("data-close-detail")) {
        closeDetail();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (!elements["favorites-modal"].hidden) {
          closeFavorites();
        } else if (!elements["detail-modal"].hidden) {
          closeDetail();
        }
      }
    });
    bindScrollSafeRange(elements["detail-start"], () => {
      const start = Number(elements["detail-start"].value);
      let end = state.detailEnd;
      if (end - start < MIN_SELECTION) {
        end = Math.min(state.detailBars.length, start + MIN_SELECTION);
      }
      setDetailView(start, end);
    });
    bindScrollSafeRange(elements["detail-end"], () => {
      const end = Number(elements["detail-end"].value) + 1;
      let start = state.detailStart;
      if (end - start < MIN_SELECTION) {
        start = Math.max(0, end - MIN_SELECTION);
      }
      setDetailView(start, end);
    });

    let resizeTimer = 0;
    const redraw = () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        drawMainChart();
        drawDetailChart();
        drawFavoritePreviewChart();
      }, 80);
    };
    if ("ResizeObserver" in window) {
      const observer = new ResizeObserver(redraw);
      observer.observe(elements["main-chart"]);
      observer.observe(elements["detail-chart"]);
      observer.observe(elements["favorite-preview-chart"]);
    }
    const handleViewportResize = () => {
      syncViewportHeight();
      redraw();
    };
    window.addEventListener("resize", handleViewportResize);
    window.addEventListener("orientationchange", handleViewportResize);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", handleViewportResize);
    }
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && liveDataIsStale()) {
        refreshLiveMarket().catch(() => {
          // 页面恢复时更新失败不影响内置行情继续使用。
        });
      }
    });
  }

  function showApplication() {
    updateMarketStatus("准备获取实时行情");
    elements["loading-screen"].hidden = true;
    elements.app.hidden = false;
    elements["stock-query"].focus();
  }

  function showFatalError(error) {
    elements["loading-text"].textContent = (
      `无法载入移动端数据：${error.message || String(error)}`
    );
    const spinner = document.querySelector(".loading-spinner");
    if (spinner) {
      spinner.hidden = true;
    }
  }

  function initialize() {
    syncViewportHeight();
    cacheElements();
    loadFavorites();
    updateFavoritesButton();
    bindEvents();
    window.setTimeout(() => {
      try {
        const payload = window.__MOBILE_DATA__;
        try {
          initializeMarketData(payload);
        } finally {
          // 解码后立即释放超大 Base64 文本，降低苹果设备的内存压力。
          if (payload && typeof payload === "object") {
            payload.bars = "";
          }
          window.__MOBILE_DATA__ = null;
          const dataScript = byId("embedded-market-data");
          if (dataScript && dataScript.parentNode) {
            dataScript.textContent = "";
            dataScript.parentNode.removeChild(dataScript);
          }
        }
        showApplication();
        window.setTimeout(() => {
          refreshLiveMarket().catch(() => {
            // 首次联网失败时自动回退到内置行情。
          });
        }, 80);
        state.liveRefreshTimer = window.setInterval(() => {
          if (!document.hidden) {
            refreshLiveMarket().catch(() => {
              // 定时更新失败时保留上一次可用行情。
            });
          }
        }, LIVE_REFRESH_INTERVAL_MS);
      } catch (error) {
        showFatalError(error);
      }
    }, 30);
  }

  // 暴露只读测试入口，便于构建后校验移动端与桌面版算法一致。
  window.__KLINE_MOBILE_TEST__ = {
    calculateIndicators,
    buildIndicatorFeatures,
    featureScore,
    compositeSimilarityScore,
    compareFeatures,
    setSimilarityFilters(filters) {
      const normalized = normalizeSimilarityFilters(filters);
      for (const filter of DEFAULT_SIMILARITY_FILTERS) {
        elements[`filter-${filter}`].checked = (
          normalized.indexOf(filter) >= 0
        );
      }
      if (state.candidateResults.length) {
        applySimilarityFilters();
      }
      return [...normalized];
    },
    loadTargetByCode(code) {
      loadTarget(code);
      return {
        name: state.target ? state.target.name : "",
        count: state.targetBars.length,
      };
    },
    select(start, end) {
      updateSelection(start, end);
      return state.selection ? [...state.selection] : null;
    },
    async search() {
      await searchSimilar();
      return state.results.map((result) => ({
        code: result.stock.code,
        score: result.score,
      }));
    },
    async refreshLive() {
      return refreshLiveMarket({ manual: false });
    },
    summary() {
      return {
        stocks: state.stocks.length,
        date: state.marketDate,
        embeddedDate: state.embeddedDate,
        liveStocks: state.liveBars.size,
        liveUpdated: Boolean(state.liveUpdatedAt),
        records: state.stocks.reduce(
          (total, stock) => total + stock.count,
          0,
        ),
      };
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {
      once: true,
    });
  } else {
    initialize();
  }
})();
