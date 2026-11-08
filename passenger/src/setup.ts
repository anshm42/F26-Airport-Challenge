import "./setup.css";
import { describeSelectors } from "./gates";

// Setup page: builds the board URL (board.html?show=…&lang=…&gates=…&source=…) without hand-editing it.
// Pre-filled from its own query string, so the board's "g" shortcut lands here with the current settings.

const params = new URLSearchParams(location.search);
const state = {
  show: params.get("show") ?? "all",
  lang: params.get("lang") ?? "en",
  gates: params.get("gates") ?? "",
  source: params.get("source") ?? "",
};

// Quick picks for Pearson's terminals and branches; anything else goes in the text field.
const QUICK_GATES: [label: string, chips: string[]][] = [
  ["Terminal 1", ["1", "1:D", "1:E", "1:F"]],
  ["Terminal 3", ["3", "3:A", "3:B", "3:C"]],
];

function segmented(name: string, options: [value: string, label: string][]): string {
  return `<div class="segmented" role="radiogroup">${options
    .map(([value, label]) => `<label><input type="radio" name="${name}" value="${value}"><span>${label}</span></label>`)
    .join("")}</div>`;
}

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
  <header>
    <span class="sign">✈</span>
    <h1>Board setup <span class="fr">| Configuration</span></h1>
  </header>
  <main>
    <form id="form">
      <section>
        <h2>Flights</h2>
        ${segmented("show", [["departures", "Departures"], ["arrivals", "Arrivals"], ["all", "Both"]])}
      </section>
      <section>
        <h2>Language</h2>
        ${segmented("lang", [["en", "English"], ["fr", "Français"]])}
      </section>
      <section>
        <h2>Gates</h2>
        ${QUICK_GATES.map(
          ([label, chips]) => `
          <div class="chips">
            <span class="chips-label">${label}</span>
            ${chips.map((c) => `<button type="button" class="chip" data-gate="${c}">${c.includes(":") ? c.split(":")[1] : "All"}</button>`).join("")}
          </div>`,
        ).join("")}
        <input id="gates" name="gates" autocomplete="off" spellcheck="false" placeholder="All gates">
        <p class="hint">Comma separated: <code>1</code> terminal · <code>1:D</code> branch · <code>1:D24</code> gate · <code>1:D20-30</code> range · <code>Remote</code> or <code>R:H01A</code> remote stands</p>
        <p class="showing">Showing: <strong id="showing"></strong></p>
      </section>
      <details>
        <summary>Advanced</summary>
        <label class="field">Data source
          <input id="source" name="source" autocomplete="off" spellcheck="false" placeholder="ws://${location.hostname}:9000/passenger/ws (the matcher, default) or mock">
        </label>
      </details>
      <div class="actions">
        <a id="open" class="open">Open board</a>
        <code id="url"></code>
      </div>
    </form>
    <aside>
      <h2>Preview</h2>
      <div class="preview"><iframe id="preview" title="Board preview" tabindex="-1"></iframe></div>
    </aside>
  </main>`;

const form = document.querySelector<HTMLFormElement>("#form")!;
const gatesEl = document.querySelector<HTMLInputElement>("#gates")!;
const sourceEl = document.querySelector<HTMLInputElement>("#source")!;
const previewEl = document.querySelector<HTMLIFrameElement>("#preview")!;

form.querySelector<HTMLInputElement>(`input[name=show][value="${state.show}"]`)?.click();
form.querySelector<HTMLInputElement>(`input[name=lang][value="${state.lang}"]`)?.click();
gatesEl.value = state.gates;
sourceEl.value = state.source;
if (state.source) form.querySelector("details")!.open = true;

const tokens = () => gatesEl.value.split(",").map((s) => s.trim()).filter(Boolean);
const isCovered = (token: string, all: string[]) => token.includes(":") && all.includes(token.split(":")[0]);
/** A whole terminal supersedes anything inside it ("1" makes "1:D" or "1:D24" redundant), so drop those. */
const normalize = (all: string[]) => [...new Set(all.filter((t) => !isCovered(t, all)))];

/** Board URL with defaults left out, so it stays short. */
function boardUrl(): string {
  const q = new URLSearchParams();
  if (state.show !== "all") q.set("show", state.show);
  if (state.lang !== "en") q.set("lang", state.lang);
  if (state.gates) q.set("gates", state.gates);
  if (state.source) q.set("source", state.source);
  const query = q.toString().replace(/%2C/g, ",").replace(/%3A/g, ":");
  return `board.html${query ? `?${query}` : ""}`;
}

let previewTimer: number | undefined;

function update() {
  const data = new FormData(form);
  state.show = String(data.get("show"));
  state.lang = String(data.get("lang"));
  state.gates = normalize(tokens()).join(",");
  state.source = sourceEl.value.trim();

  const active = new Set(tokens());
  document.querySelectorAll<HTMLButtonElement>(".chip").forEach((chip) => {
    const gate = chip.dataset.gate!;
    // While a terminal's "All" is on, its branches are unselected and can't be clicked.
    const covered = isCovered(gate, [...active]);
    chip.disabled = covered;
    chip.classList.toggle("on", active.has(gate) && !covered);
  });
  document.querySelector("#showing")!.textContent = describeSelectors(state.gates || null);

  const url = boardUrl();
  document.querySelector<HTMLAnchorElement>("#open")!.href = url;
  document.querySelector("#url")!.textContent = new URL(url, location.href).toString();
  history.replaceState(null, "", `?${new URLSearchParams(Object.entries(state).filter(([, v]) => v)).toString()}`);

  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    if (previewEl.getAttribute("src") !== url) previewEl.src = url;
  }, 300);
}

document.querySelectorAll<HTMLButtonElement>(".chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    const gate = chip.dataset.gate!;
    const current = tokens();
    gatesEl.value = normalize(current.includes(gate) ? current.filter((t) => t !== gate) : [...current, gate]).join(", ");
    update();
  }),
);
form.addEventListener("input", update);
// Tidy typed selectors once editing is done, rather than rewriting the field mid-keystroke.
gatesEl.addEventListener("change", () => {
  gatesEl.value = normalize(tokens()).join(", ");
  update();
});
form.addEventListener("submit", (e) => e.preventDefault());

// The preview renders the board at a real screen size, scaled down to fit.
const BOARD_WIDTH = 1440;
const BOARD_HEIGHT = 900;
previewEl.style.width = `${BOARD_WIDTH}px`;
previewEl.style.height = `${BOARD_HEIGHT}px`;
new ResizeObserver(([entry]) => {
  previewEl.style.transform = `scale(${entry.contentRect.width / BOARD_WIDTH})`;
}).observe(document.querySelector(".preview")!);

update();
