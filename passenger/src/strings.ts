// Board text in English and French. The title is always bilingual, like Pearson's boards, with the chosen language first.

export type Lang = "en" | "fr";
export type Show = "all" | "departures" | "arrivals";

export const STRINGS = {
  en: {
    title: { all: "Flights", departures: "Departures", arrivals: "Arrivals" },
    time: "Time",
    destination: "Destination",
    origin: "Origin",
    flight: "Flight",
    gate: "Gate",
    status: "Status",
    from: "From",
    cancelled: "Cancelled",
    delayed: (t: string) => `Delayed - ${t}`,
    revised: (t: string) => `Revised - ${t}`,
    gateChange: (g: string) => `Gate Change - was ${g}`,
    boarding: "Boarding",
    boardsIn: (m: number) => `Boards in ${m} min`,
    boardsAt: (t: string) => `Boards ${t}`,
    onTime: "On Time",
    arrived: "Arrived",
    departed: "Departed",
    updated: (t: string) => `Updated ${t}`,
    footnote: "Toronto time · all times are estimates",
    noFlights: (scope: string) => `No flights for ${scope}`,
    // Feed statuses (misc.status) arrive in English and are shown as-is.
    feedStatus: {} as Record<string, string>,
  },
  fr: {
    title: { all: "Vols", departures: "Départs", arrivals: "Arrivées" },
    time: "Heure",
    destination: "Destination",
    origin: "Provenance",
    flight: "Vol",
    gate: "Porte",
    status: "Statut",
    from: "De",
    cancelled: "Annulé",
    delayed: (t: string) => `Retardé - ${t}`,
    revised: (t: string) => `Nouvelle heure - ${t}`,
    gateChange: (g: string) => `Nouv. porte - anc. ${g}`,
    boarding: "Embarquement",
    boardsIn: (m: number) => `Embarquement dans ${m} min`,
    boardsAt: (t: string) => `Embarquement ${t}`,
    onTime: "À l'heure",
    arrived: "Arrivé",
    departed: "Parti",
    updated: (t: string) => `Mis à jour ${t}`,
    footnote: "Heure de Toronto · toutes les heures sont estimées",
    noFlights: (scope: string) => `Aucun vol pour ${scope}`,
    feedStatus: {
      boarding: "Embarquement",
      "final call": "Dernier appel",
      "gate closed": "Porte fermée",
      departed: "Parti",
      arrived: "Arrivé",
      delayed: "Retardé",
      cancelled: "Annulé",
    } as Record<string, string>,
  },
};

export function title(lang: Lang, show: Show): [primary: string, secondary: string] {
  const other: Lang = lang === "en" ? "fr" : "en";
  return [STRINGS[lang].title[show], STRINGS[other].title[show]];
}
