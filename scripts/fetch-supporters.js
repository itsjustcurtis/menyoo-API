// fetch-supporters.js
//
// Fetches active patron data from the Patreon API and writes a sanitised
// JSON file containing only tier names and patron display names.
// No emails, member IDs, or user IDs are ever written to disk.
//
// Requires Node 18+ (built-in fetch).
//
// Expected environment variables (injected as GitHub Actions secrets):
//   PATREON_CLIENT_ID
//   PATREON_CLIENT_SECRET
//   PATREON_REFRESH_TOKEN
//   PATREON_CAMPAIGN_ID

const fs = require("fs");
const path = require("path");

const {
  PATREON_CLIENT_ID,
  PATREON_CLIENT_SECRET,
  PATREON_REFRESH_TOKEN,
  PATREON_CAMPAIGN_ID,
} = process.env;

// Bail out early and loudly if any secret is missing, rather than failing
// deep inside a fetch call with a confusing error.
function requireEnv(name, value) {
  if (!value) {
    console.error(`Missing required environment variable: ${name}`);
    process.exit(1);
  }
}
requireEnv("PATREON_CLIENT_ID", PATREON_CLIENT_ID);
requireEnv("PATREON_CLIENT_SECRET", PATREON_CLIENT_SECRET);
requireEnv("PATREON_REFRESH_TOKEN", PATREON_REFRESH_TOKEN);
requireEnv("PATREON_CAMPAIGN_ID", PATREON_CAMPAIGN_ID);

const OUTPUT_PATH = path.join(__dirname, "..", "data", "supporters.json");

// Which Patreon tier titles to include in the output, and in what order.
// Any tier not listed here is ignored (e.g. if you create a test tier on
// Patreon that shouldn't show up in-game).
const KNOWN_TIERS = ["Curators", "Directors", "Supporters"];

async function refreshAccessToken() {
  const res = await fetch("https://www.patreon.com/api/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: PATREON_REFRESH_TOKEN,
      client_id: PATREON_CLIENT_ID,
      client_secret: PATREON_CLIENT_SECRET,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Token refresh failed (${res.status}): ${body}`);
  }

  const data = await res.json();
  return data.access_token;
}

async function fetchMembers(accessToken) {
  const params = new URLSearchParams({
    include: "user,currently_entitled_tiers",
    "fields[member]": "full_name,patron_status",
    "fields[user]": "full_name,vanity",
    "fields[tier]": "title,amount_cents",
    "page[count]": "100", // max page size, reduces pagination round-trips
  });

  let url = `https://www.patreon.com/api/oauth2/v2/campaigns/${PATREON_CAMPAIGN_ID}/members?${params}`;
  const allData = [];
  const allIncluded = [];

  while (url) {
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Members fetch failed (${res.status}): ${body}`);
    }

    const json = await res.json();
    allData.push(...(json.data || []));
    allIncluded.push(...(json.included || []));

    // Follow pagination cursor if present.
    url = json.links && json.links.next ? json.links.next : null;
  }

  return { data: allData, included: allIncluded };
}

function buildTierMap(included) {
  const tiersById = {};
  for (const item of included) {
    if (item.type === "tier") {
      tiersById[item.id] = {
        title: item.attributes.title,
        amountCents: item.attributes.amount_cents,
      };
    }
  }
  return tiersById;
}

function pickDisplayName(member, included) {
  // Prefer the linked Patreon user's full_name; fall back to the member's
  // own full_name (pledge name) if the user relationship is missing.
  const userRel = member.relationships.user;
  if (userRel && userRel.data) {
    const user = included.find(
      (i) => i.type === "user" && i.id === userRel.data.id
    );
    if (user && user.attributes && user.attributes.full_name) {
      return user.attributes.full_name;
    }
  }
  return member.attributes.full_name || "Unknown";
}

function groupByTier(members, included) {
  const tiersById = buildTierMap(included);
  const grouped = {};
  for (const tierName of KNOWN_TIERS) grouped[tierName] = [];

  for (const member of members) {
    if (member.attributes.patron_status !== "active_patron") continue;

    const entitled = member.relationships.currently_entitled_tiers.data || [];
    if (entitled.length === 0) continue;

    // If entitled to multiple tiers, use the highest-value one.
    let best = null;
    for (const ref of entitled) {
      const tier = tiersById[ref.id];
      if (!tier) continue;
      if (!best || tier.amountCents > best.amountCents) best = tier;
    }
    if (!best || !grouped.hasOwnProperty(best.title)) continue;

    const name = pickDisplayName(member, included);
    grouped[best.title].push(name);
  }

  // Keep member lists in a stable, readable order.
  for (const tierName of KNOWN_TIERS) grouped[tierName].sort((a, b) => a.localeCompare(b));

  return KNOWN_TIERS.map((name) => ({ name, members: grouped[name] }));
}

async function main() {
  console.log("Refreshing access token...");
  const accessToken = await refreshAccessToken();

  console.log("Fetching members...");
  const { data, included } = await fetchMembers(accessToken);
  console.log(`Fetched ${data.length} member(s).`);

  const tiers = groupByTier(data, included);

  const output = {
    last_updated: new Date().toISOString(),
    tiers,
  };

  fs.mkdirSync(path.dirname(OUTPUT_PATH), { recursive: true });
  fs.writeFileSync(OUTPUT_PATH, JSON.stringify(output, null, 2) + "\n");

  console.log(`Wrote ${OUTPUT_PATH}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
