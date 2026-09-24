/**
 * The live competitive service (2026-09-14): presence, the queue, match formation, and the
 * connect window that stands between a decided match and a played one.
 *
 * TRANSPORT: Server-Sent Events for server->client, ordinary POSTs for client->server.
 * Deliberately not WebSockets. The competitive traffic is low-rate - a queue tick, a veto
 * step, a chat line - not a game protocol, and SSE buys three things that matter here:
 * no new npm dependency in a deploy pipeline that currently has none and works; a Python
 * client that is a plain streaming GET; and clean behaviour through Railway's proxy. If a
 * later feature genuinely needs client->server push, the transport can be swapped behind
 * the same event names without the hub noticing.
 *
 * STATE: in memory, because the service runs as a SINGLE Railway replica (numReplicas 1,
 * checked 2026-09-14). That is what makes a plain Map a legitimate broadcast bus. The day
 * a second replica appears this must move to Upstash pub/sub, and the queue below will
 * silently split in two until it does - so that is a deliberate, recorded trade, not an
 * oversight. PENALTIES are the exception: a ban that evaporates on every redeploy is not a
 * ban, so those write through to Upstash and are read back on demand.
 *
 * NOTHING MAY HANG FOREVER (Sam, 2026-09-14). Every state a player cannot leave by
 * themselves carries a deadline and a timer that fires whatever else happens:
 *
 *   found      -> ACCEPT_SECONDS   -> cancelled, the players who did not accept are blamed
 *   ready      -> LOBBY_SECONDS    -> cancelled, NOBODY is blamed (the coin flip and veto
 *                                     still run client-side; a stall there is our bug,
 *                                     not a player's)
 *   connecting -> CONNECT_SECONDS  -> cancelled, only the players who never reported in are
 *                                     blamed: a medium Elo loss and a short queue ban.
 *                                     Everyone else loses NOTHING and goes back to the
 *                                     front of the queue.
 *   live       -> LIVE_SECONDS     -> closed, NOBODY is blamed. Nothing else can end a live
 *                                     match yet, so before this timer existed the match sat
 *                                     in `matches` for ever and every player sat in `inMatch`
 *                                     for ever, and `queue/join` answered 409 "you are
 *                                     already in a match" for the life of the process.
 *                                     (B-02, 2026-09-14.)
 *
 * MATCH SIZE is configurable (COMP_MATCH_SIZE, default 10) precisely so the whole flow can
 * be exercised by one person. Sam cannot summon ten players to test a queue.
 */
const crypto = require('crypto');
const identity = require('./player-identity.cjs');
const ratingLib = require('./rating.cjs');
const matchmaker = require('./matchmaker.cjs');
const networkLib = require('./network.cjs');
const relayLib = require('./relay.cjs');
const valuationLib = require('./valuation.cjs');
const progressLib = require('./progress.cjs');
const settlementLib = require('./settlement.cjs');
const combatLib = require('./combat.cjs');
const combatStorage = require('./combat-storage.cjs');
const { roundDetails } = require('./round-details.cjs');
const combatLedger = require('./combat-ledger.cjs');
const penaltyWriteLib = require('./penalty-write.cjs');
const censorLib = require('./censor.cjs');
const rankedModes = require('./ranked-modes.cjs');

const MATCH_SIZE = Math.max(1, Math.min(10, Number(process.env.COMP_MATCH_SIZE) || 10));
// THE ACCEPT WINDOW IS 30 s, NOT 20 (Sam, 2026-09-15: "lets do around 30 seconds yeah").
//
// It was 20, and 20 assumes the player's hub is drawing the button the moment the match forms.
// That assumption broke badly on Sam's machine: the hub's own snapshot was costing ~3 s per state
// change (three `tasklist` spawns on its single UI thread, fixed in hub 2.0.9), so the Accept
// button appeared with a third of the window already gone and matches were cancelling as
// "somebody did not accept" when he had accepted.
//
// The hub is fixed, but the window stays at 30 anyway, because THE CLIENT IS NOT THE ONLY THING
// THAT CAN BE SLOW: a cold start, a machine with the game hammering it, or a bad few seconds of
// network all spend the same budget - and the cost of being a second too slow is not a re-queue,
// it is a no-show penalty and an elo hit on someone who did nothing wrong. CS2 and Valorant both
// sit around 30 s or more. Being generous here costs everyone else ten seconds in the worst case;
// being stingy costs one player a ban.
const ACCEPT_SECONDS = Math.max(5, Number(process.env.COMP_ACCEPT_SECONDS) || 30);

// EACH BAN GETS A CLOCK (Sam, 2026-09-15: "add a timer for each ban selection so someone cant
// stall the lobby out forever").
//
// The veto alternates between two captains, and until now the ONLY thing bounding it was
// LOBBY_SECONDS - ten minutes, after which the whole match was cancelled as `stalled`. So one
// captain who walked away held nine other people for ten minutes and then cost them the match.
//
// WHEN IT RUNS OUT THE SERVER BANS FOR THEM. It does not cancel, and it does not skip the turn:
// cancelling punishes nine people for one person's tea break, and skipping would hand the staller
// an advantage (their opponent burns bans while they burn none, and the map pool tilts). Banning
// on their behalf keeps the veto's shape exactly - same number of bans, same alternation, same
// map count at the end - and the only thing they lose is the choice they declined to make.
//
// The pick is RANDOM from what is left rather than "the first one", so a captain cannot farm a
// predictable auto-ban by timing out on purpose.
const BAN_SECONDS = Math.max(5, Number(process.env.COMP_BAN_SECONDS) || 30);

// SO DOES EVERY OTHER CHOICE IN THE LOBBY (Sam, 2026-09-16: "too many points where a user could
// infinitly stall the lobby").
//
// The ban clock above fixed ONE of four places a captain can simply not click. The coin call, the
// side/last-ban choice and the attack/defend pick were all unbounded, and each of them is worse
// than a stalled ban was: the veto at least ran on a clock once it started, while a captain who
// never called heads held the lobby at its very first screen until LOBBY_SECONDS cancelled the
// whole match. The hub's own watchdog made that visibly worse - it gives each lobby stage 90 s
// and then drops THAT player out of a match nobody did anything wrong in.
//
// Same rule as the ban, for the same reasons: when it runs out the server DECIDES FOR THEM, at
// random, and the lobby carries on with its shape intact. Random and not a default, because a
// fixed fallback ("the winner always takes side") is an outcome a captain could farm by refusing
// to click - stalling has to be strictly neutral, never profitable.
const PICK_SECONDS = Math.max(5, Number(process.env.COMP_PICK_SECONDS) || 30);

// How long the coin stays in the air (Sam, 2026-09-16: "let the coinflip also flip slower and take
// slightly more time... i want the users to actually see what the coin lands on").
//
// This is a REAL SERVER STAGE, not a client animation. It used to be neither: flipCoin decided the
// face and moved straight to 'choice', and only the captain who pressed the button saw a spin at
// all - their hub faked a local 'flipping' stage. The other nine went from "waiting for X to flip"
// to the result line with nothing in between, so the one moment in the lobby worth watching was
// the one moment nine of them never saw.
//
// Holding it here means every client is spinning the same coin for the same window and lands on
// the same face. It is the only stage NOBODY can stall - there is nothing to click - so its clock
// is a presentation hold rather than a deadline.
const FLIP_SECONDS = Math.max(1, Number(process.env.COMP_FLIP_SECONDS) || 4);

// How many rounds a single match may record. Bodybomb is twelve; this is only here so a garbled
// or hostile score report cannot grow an array on a live match for ever.
const ROUND_KEEP = 64;

// TEAM KILLS (Sam, 2026-09-15). The pak reports every team kill it sees with the context needed
// to judge it; the judging is here, because what counts as malicious has to be tuned against real
// matches and a rule baked into a cooked asset can only be changed by shipping a new pak.
//
// FRIENDLY FIRE IS ON IN BODYBOMB, so a team kill is not by itself an offence - it is Tuesday.
// The whole problem is separating the accident from the intent, and the signals that do it are
// the ones an accident cannot produce:
//
//   early      a kill in the first few seconds of a round. Nobody has engaged an enemy yet, so
//              there is no crossfire to have been caught in.
//   no_enemy   nobody on the other team is alive. There was nothing else in the world to shoot.
//   repeat     the same teammate, twice in one match. Crossfire does not pick favourites.
//   volume     three or more in a match. Two is a bad night with a grenade; three is a pattern.
//
// The first three are strong enough to act on alone. `volume` is the accumulating one.
const TK_EARLY_SECONDS = Math.max(0, Number(process.env.COMP_TK_EARLY_SECONDS) === 0
  ? 0 : Number(process.env.COMP_TK_EARLY_SECONDS) || 3);
const TK_MATCH_LIMIT = Math.max(2, Number(process.env.COMP_TK_MATCH_LIMIT) || 3);
// How many team kills a single match may record, so a garbled or hostile report cannot grow an
// array without bound on a live match.
const TK_KEEP = 200;

// REPORTS (Sam, 2026-09-15). A player can report anyone they were in a match with, from the
// lobby, the live screen, the result or their match history.
//
// THE REASONS ARE A CLOSED LIST, and a short one. A free-text box would be a moderation queue
// nobody reads and a place to put abuse, which is the thing being reported. Every value here maps
// to something a human reviewing the report can actually check.
const REPORT_REASONS = ['cheating', 'text_abuse', 'voice_abuse', 'afk', 'griefing',
                        'team_killing', 'smurfing', 'other'];
// One report per reporter, per target, PER MATCH. Not per lifetime: the same person griefing you
// in three different matches is three pieces of evidence, and collapsing them would hide a
// pattern. Within one match a second report adds nothing except a way to inflate a count.
// FRIENDS (Sam, 2026-09-15). Keyed by SteamID64 and persisted, so a player who signs in on
// another machine still has their list - which is the whole requirement. Parties are deliberately
// ephemeral and die with a redeploy; friends must not.
//
// Reached by a FRIEND CODE rather than by steam id, the same way parties are joined. A code can be
// hidden, copied and regenerated, which means a player who put theirs on a stream can cut off the
// requests without losing the friends they already have - and nobody has to hand out an id that
// identifies them everywhere else on Steam.
// HOW LONG A FRIEND CODE IS, and why it is not six like a party code.
//
// A party code only has to be unique among the parties that exist RIGHT NOW - a few dozen - and
// it is checked against the live `parties` map, which is the authority. A friend code has to be
// unique among every player who has ever had one, for as long as they keep it, and the authority
// for that lives in Upstash rather than in this process's memory.
//
// Six characters of a 32-character alphabet is 32^6, about a billion. That sounds like plenty
// until you remember the birthday problem: a collision becomes more likely than not at roughly
// the square root, which is ~33,000 codes. For a community that hopes to grow, that is a bug with
// a date on it.
//
// Ten characters is 32^10, about 1.1 quadrillion, with the same birthday maths putting the first
// likely collision past 33 million codes. Two more characters to read out, and the problem stops
// existing.
const FRIEND_CODE_LEN = Math.max(6, Number(process.env.COMP_FRIEND_CODE_LEN) || 10);
// WHO MAY SEE THE ADMIN CONSOLE (Sam, 2026-09-15: "lets do steam id auth for the admin console").
//
// A list of SteamID64s, and nothing else - no password, no second login, no new secret to leak.
// The alternative on the table was a shared password in an environment variable, which is one
// string standing between a public URL and every player's report history: it cannot be rotated
// without telling everyone, it cannot be revoked for one person, and it leaves no trace of WHO
// looked. Steam ids have none of those problems, because the sign-in already exists and already
// proves identity.
//
// EMPTY MEANS NOBODY. Not "everybody", not "the first person who asks" - an unset variable is the
// state a fresh deploy is in, and that state must be closed.
const ADMIN_IDS = new Set(String(process.env.COMP_ADMIN_STEAM_IDS || '')
  .split(/[\s,]+/).map((x) => x.trim()).filter((x) => identity.validSteam(x)));

const MAX_FRIENDS = Math.max(1, Number(process.env.COMP_MAX_FRIENDS) || 200);
const MAX_FRIEND_REQUESTS = Math.max(1, Number(process.env.COMP_MAX_FRIEND_REQUESTS) || 100);

const REPORT_KEEP = 200;              // per target, newest first
const REPORT_TTL_SECONDS = Math.max(
  24 * 3600, Number(process.env.COMP_REPORT_TTL_SECONDS) || 180 * 24 * 3600);

// BUG REPORTS (Sam, 2026-09-16): "a screen that enables the user to report a bug ... that bug will
// then get sent to the admin console with their steam id and name".
//
// NOT A PLAYER REPORT, and deliberately not stored like one. A player report is an accusation
// about a person, so it is keyed by the person and every question anyone asks of it is about
// them. A bug report is about the APP: nobody ever asks "what has this player filed", they ask
// "what is broken", so it is ONE list, newest first, and the reporter is a column on the row
// rather than the key.
//
// WHO IT IS FROM IS NOT ASKED FOR. It comes off the authenticated account, exactly like every
// other route here - a free-text "your steam id" box would be a box to lie in, and the one thing
// a bug report has to be good for is going back to the person who filed it.
const BUG_TEXT_MAX = 2000;            // characters; longer is truncated, not refused
const BUG_KEEP = 300;                 // rows kept, newest first
const BUG_TTL_SECONDS = Math.max(
  24 * 3600, Number(process.env.COMP_BUG_TTL_SECONDS) || 180 * 24 * 3600);
// ONE EVERY FIVE SECONDS, per account (Sam). The hub's own screen disables its button for the
// same five seconds, but the button is not the guard: this is. Anything holding a bearer token
// can POST this endpoint in a loop, and what is on the other end of that loop is a list a human
// has to read.
const BUG_COOLDOWN_MS = Math.max(0, Number(process.env.COMP_BUG_COOLDOWN_MS) || 5000);

// ---------------------------------------------------------------- chat, and how long it is kept
//
// CHAT IS NOT KEPT FOREVER, AND IT IS NOT THROWN AWAY EITHER (Sam, 2026-09-16: "make sure not to
// store chat in the storage forever. it would be good to have it stored for a while so we can go
// back and see chat logs of reported users").
//
// Those two pull in opposite directions, so the retention is not one number, it is a rule:
//
//   * A transcript nobody complained about lives CHAT_TTL_SECONDS - ONE WEEK (Sam, 2026-09-16:
//     "lets make the chat logs stay in storage for a week"). Long enough to look into something
//     that happened over a weekend, short enough that the service is not sitting on a permanent
//     record of everything everybody has ever typed.
//   * The moment somebody is REPORTED out of a match, that match's transcript is pushed out to
//     REPORT_TTL_SECONDS, so it lasts exactly as long as the report a moderator will read it
//     next to. See keepChatForReport, called from reportPlayer.
//
// So the common case expires quickly and the case the log exists FOR does not. The extension only
// ever lengthens a TTL; a second report on the same match cannot shorten the first one's.
//
// CHAT_KEEP bounds one match's transcript. Ten people over a lobby and a match will not come near
// it; the cap is there so a script cannot make one key grow without limit.
const CHAT_KEEP = Math.min(2000, Math.max(50, Number(process.env.COMP_CHAT_KEEP) || 500));
const CHAT_TTL_SECONDS = Math.max(3600, Number(process.env.COMP_CHAT_TTL_SECONDS) || 7 * 24 * 3600);
const CHAT_MAX_CHARS = 200;           // the hub's input caps here too (MAX_CHAT_CHARS)
// ONE MESSAGE EVERY HALF SECOND, per account (Sam, 2026-09-16: "a user can only submit 1 message
// ever half second"). A broadcast endpoint needs a floor on how fast one person can reach nine
// others, and 500 ms is slow enough to stop a flood while sitting well under the ~150 ms a human
// needs to type even a short line - nobody chatting normally will ever meet it.
//
// It is enforced HERE and not in the hub, for the reason everything else in this file is: the hub
// runs on the sender's machine. The hub does not pre-empt it either - a client that guesses the
// gap would have to stay in step with this number, and a refusal that says "Slow down." is a
// better answer than two copies of a clock disagreeing.
const CHAT_MIN_GAP_MS = Math.max(0, Number(process.env.COMP_CHAT_MIN_GAP_MS) || 500);

// The other team has no names in the pre-round lobby (hub/censor.py's sibling rule, in
// hub/competitive.py ENEMY_CALLSIGNS): a player who can read the enemy roster can dodge one
// specific person. The HUB draws call signs, but the hub only knows what it is sent - so the
// PERSONA MUST NOT BE SENT. This is the same list, in the same order, and chatNameFor resolves
// it per recipient before the line goes out.
const ENEMY_CALLSIGNS = ['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo',
                         'Foxtrot', 'Golf', 'Hotel', 'India', 'Juliett'];

// ---------------------------------------------------------------- the player directory
//
// Sam, 2026-09-16: "a player search either through steamid or steam name ... each player thats
// ever logged on being a row and the columns have various info like games played, games won,
// deaths, kills, reports, rank, time played, with really any possible columns possible".
//
// EVERY ROW CARRIES EVERY COLUMN. Which of them are drawn is the moderator's business, kept in
// their own presets (server/admin.cjs), so turning one on costs no request and no deploy. This
// table is the whole vocabulary: a key nothing here names cannot be asked for, cannot be sorted
// on and cannot be saved into a preset.
//
//   type   how the page draws it, and nothing more
//          name/id/text  a string        num/ratio/pct  a figure, right-aligned
//          time          a duration      date           an instant, as "3d ago"
//          status/rank/ban  their own small renderers
const PLAYER_COLUMNS = [
  { key: 'persona', label: 'Name', type: 'name', text: true },
  { key: 'player_id', label: 'Player ID', type: 'id', text: true },
  { key: 'account_type', label: 'Account', type: 'text', text: true },
  { key: 'account_id', label: 'Lights Out account ID', type: 'id', text: true },
  { key: 'steam_login_id', label: 'Steam sign-in ID', type: 'id', text: true },
  { key: 'game_steam_id', label: 'Last game Steam ID', type: 'id', text: true },
  { key: 'account_created', label: 'Account created', type: 'date' },
  { key: 'status', label: 'Status', type: 'status', text: true },
  { key: 'rank_name', label: 'Rank', type: 'rank', text: true },
  { key: 'rr', label: 'RR', type: 'num' },
  { key: 'progress', label: 'RR total', type: 'num' },
  // The hidden number. It is hidden from PLAYERS (docs/ranks.md); a moderator asking why the
  // matchmaker keeps pairing two accounts is asking about exactly this.
  { key: 'mmr', label: 'MMR', type: 'num' },
  { key: 'matches', label: 'Rated', type: 'num' },
  { key: 'wins', label: 'Won', type: 'num' },
  { key: 'losses', label: 'Lost', type: 'num' },
  { key: 'win_rate', label: 'Win %', type: 'pct' },
  { key: 'played', label: 'Played', type: 'num' },
  { key: 'kills', label: 'Kills', type: 'num' },
  { key: 'deaths', label: 'Deaths', type: 'num' },
  { key: 'kd', label: 'K/D', type: 'ratio' },
  { key: 'kpr', label: 'Kills/round', type: 'ratio' },
  { key: 'team_kills', label: 'Team kills', type: 'num' },
  { key: 'clutches', label: 'Clutches', type: 'num' },
  { key: 'rounds', label: 'Rounds', type: 'num' },
  { key: 'seconds', label: 'Time played', type: 'time' },
  { key: 'abandons', label: 'Abandons', type: 'num' },
  { key: 'no_shows', label: 'No-shows', type: 'num' },
  { key: 'reports', label: 'Reports', type: 'num' },
  { key: 'reporters', label: 'Reporters', type: 'num' },
  { key: 'reports_made', label: 'Reports filed', type: 'num' },
  { key: 'cheater_score', label: 'Cheater score', type: 'suspicion' },
  { key: 'banned', label: 'Banned', type: 'ban' },
  { key: 'sessions', label: 'Hub connections', type: 'num' },
  { key: 'first_seen', label: 'First seen', type: 'date' },
  { key: 'last_seen', label: 'Last seen', type: 'date' },
  { key: 'last_match', label: 'Last match', type: 'date' },
  { key: 'last_map', label: 'Last map', type: 'text', text: true },
  { key: 'hub', label: 'Hub version', type: 'text', text: true },
  { key: 'mode', label: 'Mode version', type: 'text', text: true },
  // NOT A FIELD, and the one column that is not. It is in this table anyway because everything
  // about which columns a moderator sees lives in their presets, and a row of buttons that could
  // not be turned off would be the one thing on the page they had no say over.
  { key: 'actions', label: 'Actions', type: 'actions', noSort: true },
];

// The most a hand-set MMR may be. Not a rule about players - nobody reaches it - but about
// typing: an admin who means 1400 and hits an extra zero should be told, not obeyed.
const ELO_CEILING = Math.max(1000, Number(process.env.COMP_ELO_CEILING) || 5000);

/** What a page asks for when it asks for nothing, and the most it may ask for. The cap is not
 *  about the database - the whole directory is one read - but about the page: fifty thousand
 *  rows of HTML is a browser tab that stops responding, and a moderator who wanted one player
 *  should type their name rather than scroll. */
const DIRECTORY_PAGE = Math.max(10, Number(process.env.COMP_DIRECTORY_PAGE) || 250);
const DIRECTORY_MAX = Math.max(DIRECTORY_PAGE, Number(process.env.COMP_DIRECTORY_MAX) || 2000);

// ENFORCEMENT IS OFF UNTIL THE TEAMS ARE PROVEN, and that is not caution for its own sake: this
// compares TeamID, and the pak now WRITES TeamID itself (the team sweep). If the sweep is wrong,
// every enemy kill looks like a team kill and the hub bans people for playing the game. Until a
// real match has shown the assignment landing correctly, every verdict is recorded and nobody is
// punished. COMP_TK_ENFORCE=1 turns it on.
// Read at CALL time, not at module load. Partly so this can be flipped without reasoning about
// when the process last restarted, but mostly so it is testable: a constant frozen at import is a
// switch no test can turn, and "the penalty path works when enabled" is the one thing about this
// feature that must not go unproven while it sits disabled.
function queuePenaltiesPaused() { return process.env.COMP_QUEUE_PENALTIES_PAUSED === '1'; }
// Keep connection testing penalty-free when verified team-kill enforcement is enabled.
// The global pause remains an emergency stop for every conduct penalty.
function noShowPenaltiesPaused() {
  return queuePenaltiesPaused() || process.env.COMP_NO_SHOW_PENALTIES_PAUSED === '1';
}
function tkEnforcing() { return !queuePenaltiesPaused() && process.env.COMP_TK_ENFORCE === '1'; }

// MATCHMAKING RUNS ON A CLOCK (Sam, 2026-09-15), not only when somebody joins.
//
// It used to run on `queue/join` alone, which was fine when a match was "are there ten of you"
// - that answer cannot change while nobody joins. It is wrong now that a unit's willingness to
// accept a gap WIDENS with how long it has waited (matchmaker.toleranceFor): eleven people can
// be sitting in a queue that nobody else joins, all of them ten seconds away from accepting
// each other, and with no tick nothing would ever ask again. So the tick asks.
//
// Two seconds is under the resolution of anything a player notices and far above anything the
// arithmetic costs. `tryFormMatch` is still called on join as well, so a match that CAN form
// immediately still does - which is what keeps the two-player test suite instant.
const MATCH_TICK_MS = Math.max(250, Number(process.env.COMP_MATCH_TICK_MS) || 2000);

// NEVER PRICE A MATCH BEFORE IT IS PLAYED (Sam, 2026-09-15): "do not tell the user how much
// they will gain or lose beforehand, only afterwards."
//
// The server knows the stakes from the moment the match forms - `quality`, the two team
// ratings and the win probability are all sitting on the match object - and none of it goes
// out to a client before the result. Showing it would turn the accept screen into a decision
// about rank instead of a decision about playing, and a player who can see a cheap match has
// a reason to dodge an expensive one. The one place a number is allowed out is the result,
// and even there it is arrows rather than points (rating.arrowsFor).
//
// In practice this is a rule about `match_found`, `match_ready`, `lobby`, `match_connecting`
// and `match_live`: none of those payloads may grow a stakes field.

// A party always lands on the same team (memory: C10), so it can never be bigger than a team,
// and it can never be bigger than the match it must fit into - hence the cap is floored by
// MATCH_SIZE too. With COMP_MATCH_SIZE=1 the cap is 1: a party exists but nobody can join, which
// is exactly right for the one-person queue test; the two-account run sets COMP_MATCH_SIZE=2.
const TEAM_SIZE = 5;
const MAX_PARTY = Math.max(1, Math.min(TEAM_SIZE, Number(process.env.COMP_MAX_PARTY) || TEAM_SIZE, MATCH_SIZE));

// A member whose LAST stream closes is not dropped from their party at once: a brief reconnect
// must be a no-op (handleStream clears the timer). Only when this grace has run without them
// coming back are they actually removed. Floored low so the test suite runs fast.
const PARTY_GRACE_SECONDS = Math.max(2, Number(process.env.COMP_PARTY_GRACE_SECONDS) || 60);

// HOW LONG A PARTY INVITE STANDS. A party is an ephemeral, right-now thing - the code is minted
// when it is created and dies with it - so an invite that outlived the evening would offer a seat
// in a party that stopped existing hours ago. Three minutes is long enough to alt-tab back from
// the game and short enough that the inbox is never a list of the dead. Expiry is enforced on
// ACCEPT as well as by the timer, so a clock that never fired cannot let a stale one through.
const PARTY_INVITE_SECONDS = Math.max(10, Number(process.env.COMP_PARTY_INVITE_SECONDS) || 180);

// A cap per inbox, for the same reason friend requests have one: an invite is a thing somebody
// else can put on your screen, so there has to be a number it stops at.
const MAX_PARTY_INVITES = Math.max(1, Number(process.env.COMP_MAX_PARTY_INVITES) || 20);

// The same 31-character set the hub uses (competitive.py PARTY_CODE_ALPHABET) and auth.cjs's
// randomCode: I/O/0/1 are left out so a code read aloud or off a stream is never ambiguous, and
// normalise_party_code on the hub round-trips a code minted here.
const PARTY_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';

// EVERY PATH THIS MODULE OWNS, in one list, because server.cjs has to know it too.
//
// 2026-09-15: the friends tab showed "Not found." and no friend code. The routes existed here and
// were exercised by the tests, which call route() directly - but server.cjs only forwarded
// /api/live*, /api/queue/*, /api/match/* and /api/party/* to this module, so /api/friends/*,
// /api/report and /api/leaderboard fell through its router to a 404. A second, hand-copied list of
// paths in the caller is what made a fully-implemented feature dead on arrival, so there is one
// list now and `owns()` is what server.cjs asks. ADD A ROUTE HERE OR IT IS NOT REACHABLE.
const NEEDS_AUTH = ['/api/match/concede', '/api/messages', '/api/messages/thread', '/api/messages/send', '/api/messages/read', '/api/messages/block', '/api/tournament', '/api/tournament/register', '/api/tournament/support', '/api/match/combat-warning', '/api/live', '/api/queue/join', '/api/queue/leave',
                    '/api/network/relay', '/api/network/profile', '/api/network/peers',
                    '/api/network/signal', '/api/network/pings',
                    '/api/match/accept', '/api/match/leave',
                    '/api/match/coin', '/api/match/choose', '/api/match/side', '/api/match/ban',
                    '/api/match/connecting', '/api/match/connected',
                    '/api/match/chat', '/api/match/void-vote',
                    '/api/report',
                    '/api/bug',
                    '/api/leaderboard', '/api/ranked/profile',
                    '/api/admin/overview', '/api/admin/player',
                    '/api/admin/ban', '/api/admin/unban', '/api/admin/chat',
                    '/api/friends/list', '/api/friends/code', '/api/friends/request',
                    '/api/friends/accept', '/api/friends/decline', '/api/friends/cancel',
                    '/api/friends/remove',
                    '/api/match/history', '/api/match/completion',
                    '/api/party/create', '/api/party/join', '/api/party/leave',
                    '/api/party/refresh-code',
                    '/api/party/invite', '/api/party/invite/accept', '/api/party/invite/decline'];

// /api/live/stats is the one route that answers before the auth check, so it is not in NEEDS_AUTH
// - but it is still ours, and server.cjs still has to hand it over.
const PUBLIC_PATHS = ['/api/live/stats'];

/** Does this module handle `pathname`? The question server.cjs asks before delegating. */
function owns(pathname) {
  return NEEDS_AUTH.includes(pathname) || PUBLIC_PATHS.includes(pathname);
}

// ---------------------------------------------------------------- the version gate
//
// QUEUEING NEEDS THE CURRENT BUILD. BEING IN A MATCH DOES NOT (Sam, 2026-09-15).
//
// Ten people in one match have to agree about the gamemode they are playing and the hub that
// drives it, so the queue is shut to anybody who is not on the release this server is publishing.
// But a release lands WHILE matches are running - Railway redeploys on every push - and the
// players in them did nothing wrong. So the gate hangs off ONE route, /api/queue/join, and off
// nothing else: the stream, accept, the coin flip, the veto, the connect window, the result and
// every party route answer an out-of-date hub exactly as they did before. A match that was legal
// when it formed finishes.
//
// THE REQUIREMENT IS THE CATALOGUE THIS SAME SERVER SERVES (server/public/catalogue.json: the
// hub's version, and the ranked gamemode's). Publishing IS raising the bar, and there is no
// second list to fall out of step with it - which is the mistake `owns()` above exists to
// remember.
//
// COMP_VERSION_GATE has three settings, and WHICH ONE IS THE DEFAULT IS A RELEASE-ORDER
// DECISION, not a taste one:
//
//   off        no gate. The escape hatch, because the failure mode of a catalogue typo is
//              "nobody on earth can queue" and the fix for that must not be another deploy.
//   lenient    legacy compatibility. A hub that reports an OLDER version is refused; one that reports
//              NOTHING is let through.
//   strict     the default. A hub that reports nothing is refused too.
//
// Current ranked clients must report both app and gamemode versions.
const GATE_MODE = (() => {
  const raw = String(process.env.COMP_VERSION_GATE || '').trim().toLowerCase();
  if (raw === 'off' || raw === '0' || raw === 'false') return 'off';
  if (raw === 'strict' || raw === '1' || raw === 'true') return 'strict';
  if (raw === 'lenient') return 'lenient';
  return 'strict';
})();
const VERSION_GATE = GATE_MODE !== 'off';

// The gamemode ranked runs on, and so the one whose version is checked. Matches
// hub/competitive.py COMPETITIVE_MODE_ID.
const GATED_MODE_ID = process.env.COMP_GATE_MODE_ID || 'BB5';

/** '2.0.18' -> [2, 0, 18]. A chunk that does not start with digits counts as 0; an empty or
 *  missing version is [], which versionOlder reads as "older than anything". */
function versionParts(v) {
  const s = String(v == null ? '' : v).trim();
  if (!s) return [];
  return s.split('.').map((chunk) => {
    const m = /^(\d+)/.exec(chunk);
    return m ? Number(m[1]) : 0;
  });
}

/**
 * Is `have` STRICTLY older than `need`? The mirror of hub/catalogue.py version_newer(need, have),
 * and the whole test the gate makes.
 *
 *   no requirement  -> false. An unreadable catalogue opens the queue, it does not close it.
 *   nothing said    -> true.  A hub that does not report a version predates the release that
 *                             started reporting one, so it is by definition behind.
 *   equal or newer  -> false. A dev build ahead of the catalogue queues.
 */
function versionOlder(have, need) {
  const want = versionParts(need);
  if (!want.length) return false;
  const got = versionParts(have);
  if (!got.length) return true;
  for (let i = 0; i < Math.max(got.length, want.length); i += 1) {
    const a = got[i] || 0;
    const b = want[i] || 0;
    if (a !== b) return a < b;
  }
  return false;
}

// How long the client-side lobby (coin flip, side/ban choice, map veto) may take before the
// server decides something has gone wrong and releases everyone. Generous on purpose: this
// is a safety net, not a shot clock. No blame is assigned when it fires.
//
// The floors on these three are low so the test suite can drive a whole no-show in seconds.
// They exist to catch a typo (COMP_CONNECT_SECONDS=0), not to defend a policy - the policy
// is the DEFAULT next to each one.
const LOBBY_SECONDS = Math.max(10, Number(process.env.COMP_LOBBY_SECONDS) || 600);

// The join/connect window. Sam's original figure was 3 minutes, but a NON-HOST cannot even begin
// to join until the host's game is up and stamped (the lobby pak gets one search per launch), so
// the window a joiner actually gets must be measured from host-ready, not from when the window
// opened. This is that window, 5 minutes: it arms at beginConnect (long enough for the host to get
// its game up, else the match dies here and the missing host pays), and reportConnected re-bases it
// to a fresh CONNECT_SECONDS the moment the host reports in, so a joiner still loading is never
// no-show-penalized before their five minutes are up.
const CONNECT_SECONDS = Math.max(5, Number(process.env.COMP_CONNECT_SECONDS) || 300);

// ...and the no-show pays for it: "the person who didnt connect will lose a medium size of
// elo and get a 5 [minute] queue ban". This is a LIGHTER offence than abandoning a live
// match, which has its own ladder (10m/30m/1h/1d/1w/permanent - memory section 2, C6-C9)
// and is not implemented yet. Both hang off the same penalty store below.
const NO_SHOW_BAN_SECONDS = Math.max(5, Number(process.env.COMP_NO_SHOW_BAN_SECONDS) || 300);

// IT IS RR NOW, NOT ELO. The points came off the matchmaking rating until 2026-09-16, which
// meant the only part of the punishment a player could actually be shown was the clock: the rest
// landed on a number we never print and then seeped into their rank over the following matches,
// by which time nothing connected it to the match they walked out of. Sam: "lets swap it so a
// penalty doesnt cost any matchmaking rating and instead costs RR." So it is RR, it comes off the
// visible total, and it comes off the moment the penalty lands (progress.penalise).
//
// COMP_NO_SHOW_ELO still works as the dial's old name - it is set in deployments and in the test
// harness, and a rename that silently reverted the cost to the default would be a worse bug than
// the one this fixes.
//
// 15 RR, down from 25 (Sam, 2026-09-16).
const NO_SHOW_RR = Math.max(0, Number(process.env.COMP_NO_SHOW_RR) === 0 ? 0
  : Number(process.env.COMP_NO_SHOW_RR)
    || (Number(process.env.COMP_NO_SHOW_ELO) === 0 ? 0 : Number(process.env.COMP_NO_SHOW_ELO))
    || 15);

// A FLAT five minutes is farmable: miss the window, wait it out, miss the next one, forever.
// So the ban climbs on repeats (Sam, 2026-09-14), as MULTIPLES of the first rung, which keeps
// Sam's five minutes as the first offence and lets the tests run the whole ladder in seconds:
//
//     5 min -> 15 min -> 30 min -> 1 h, and 1 h from then on.
//
// It stops at ONE HOUR (Sam, 2026-09-16, softened from four). Never turning up is rude; it is
// not the same as walking out of a live match in front of nine people, and that offence owns
// the days-and-weeks end of the scale (C6-C9). An hour is already long enough that farming the
// window costs more than it is worth, and short enough that a bad evening does not end the
// night for somebody we would rather keep. The ELO loss stays flat at "medium" - the ban is
// what escalates.
const NO_SHOW_LADDER = [1, 3, 6, 12];

// ...and it forgives. One rung comes off for every clean day, the same shape as the abandon
// ladder's decay (C8), so a bad evening is not carried around for a month.
const NO_SHOW_DECAY_SECONDS = Math.max(60, Number(process.env.COMP_NO_SHOW_DECAY_SECONDS) || 24 * 3600);

// ---------------------------------------------------------------- match history
// Sam, 2026-09-14: the Competitive tab gets a Match History sub-tab, and it shows REAL
// matches from the first day rather than a preview. So every match that ends is written
// through to Upstash the same way a penalty is, for the same reason: a history that dies
// on every redeploy is not a history.
//
// Two keys per match, both optional (Upstash is allowed to be absent - see upstashCmd):
//   <prefix>match:<matchId>    the whole record, shared by all ten players
//   <prefix>history:<steamId>  a LIST of compact rows, newest first, trimmed to HISTORY_KEEP
//
// What is NOT here yet: the score, who won, and the Elo delta. The gamemode cannot report a
// scoreboard (memory section 3), so those three fields are written as null and the tab shows
// them as pending. Everything else - who played, on which team, which map, who no-showed and
// what it cost them - the server already knows for certain, and that is what makes this worth
// building now instead of after the rank service.
const HISTORY_KEEP = Math.min(500, Math.max(1, Number(process.env.COMP_HISTORY_KEEP) || 50));
const HISTORY_TTL_SECONDS = Math.max(3600, Number(process.env.COMP_HISTORY_TTL_SECONDS) || 180 * 24 * 3600);
// A row links to a record, so the record must NOT die first or the detail pop-up 404s on a row
// the list is still drawing. Whatever the two env vars say, the record outlives the list.
const MATCH_TTL_SECONDS = Math.max(HISTORY_TTL_SECONDS,
  Math.max(3600, Number(process.env.COMP_MATCH_TTL_SECONDS) || 180 * 24 * 3600));
// The in-memory archive is a fallback for a process with no Upstash, not a cache. Capped so a
// long-lived Railway replica cannot grow without bound.
const ARCHIVE_MEMORY_KEEP = 300;

// ---------------------------------------------------------------- surviving a redeploy
// A PUSH MUST NOT KILL A LIVE MATCH (Sam, 2026-09-15).
//
// Railway redeploys on every push, and until now every live match lived in this process's
// memory and nowhere else - so a release landed and ten people mid-veto were told, by a
// brand-new container, that they were in no match at all. The hub already survives the stream
// dropping (handleStream replays whatever phase the match is in the moment they reconnect);
// what it could not survive was the SERVER forgetting.
//
// So a live match is written through to Upstash exactly as a penalty, a rating and a finished
// match already are, and a starting container reads them back and re-arms their clocks from the
// ABSOLUTE deadlines it stored. The replay path the hub already has does the rest - from its
// side a redeploy now looks like a slightly long reconnect.
//
// This is the LIVE copy and it is not the archive: `match:<id>` is the finished record that
// history reads, kept for months, and it must not be disturbed by this. Two keys, both optional
// because Upstash is allowed to be absent (a local run and the whole test suite have none, and
// there a redeploy is a restart of something with no players on it):
//   <prefix>live:match:<matchId>   the whole live match object, timers stripped
//   <prefix>live:matches           a SET of the ids above, so a boot knows what to look for
//
// The copy is thrown away the moment the match ends, so a boot can never resurrect a finished
// one; the TTL is the backstop for the case where the process died between the two.
//
// WHAT THIS DOES NOT SOLVE, written down rather than discovered later. For a few seconds during a
// deploy both containers exist, and both may hold the same match's clocks - the new one because it
// just read it back, the old one because it has not been stopped yet. Two things keep that small
// and survivable: server.cjs stops the old one's clocks on SIGTERM (which is Railway switching
// over, and is the first thing that happens once the new container takes traffic), and this module
// is only built on the first competitive request a container serves, so the new one arms nothing
// until traffic has already moved to it. What is left is a window of seconds in which an expiry
// could be judged twice, and RECOVERY_GRACE_SECONDS below is what makes it harmless: the clock a
// recovered match comes back on is never about to fire.
//
// THE QUEUE AND PARTIES ARE NOT KEPT. Losing them costs one press of Find match and one re-shared
// party code, and the hub already handles both (a stream that drops while queued puts the player
// back to idle rather than pretending). A MATCH is the thing that cannot be re-made, and it is
// what this exists for.
const LIVE_STATE_TTL_SECONDS = Math.max(3600, Number(process.env.COMP_LIVE_STATE_TTL_SECONDS)
  || 6 * 3600);

// NOBODY PAYS FOR THE HANDOVER. A recovered match's deadline is pushed out to at least this far
// away, because the seconds that ran while the service was restarting are not seconds the player
// had. Without it a push landing 20 seconds into somebody's 30-second accept window would judge
// them - and charge them Elo and a queue ban - for a window they spent watching a hub reconnect.
//
// It is a FLOOR, not an extension: a match with four minutes left keeps its four minutes.
const RECOVERY_GRACE_SECONDS = Math.max(5, Number(process.env.COMP_RECOVERY_GRACE_SECONDS) || 45);

// A match is as long as it is, but it is not INFINITE. A best-of-13 Bodybomb match runs well
// under an hour, so three hours in `live` means the gamemode never reported back - and until
// there is a result service, NOTHING else ever will. This is the deliberately absurd ceiling
// the design asks for: it exists so a finished match cannot hold ten players out of the queue
// for ever, not to cut anyone's game short. The hub's own `live` watchdog is set LONGER than
// this on purpose, so the server is always the one that lets go first.
const LIVE_SECONDS = Math.max(5, Number(process.env.COMP_LIVE_SECONDS) || 3 * 3600);

// ---------------------------------------------------------------- the collection window
// HOW LONG THE MATCH STAYS OPEN AFTER IT IS WON, so the last stats can land.
//
// The score limit being reached is not the moment to settle, and the 2026-09-15 match is why.
// The real end (ABodycamGameState::OnMatchEnded, which the pak reports as ch_exit_armed) was at
// 20:41:36.5; the score report that told us was at 20:41:48.2, nearly TWELVE SECONDS later,
// because that report is on a 15 s timer. Meanwhile the stats sweep visits one player every 3 s,
// so with ten players a given player's numbers are up to 30 s old. Settling on the score report
// therefore settles on stats that are stale by up to half a minute - and the game goes on sending
// the FINAL, freshest numbers for another 33 s afterwards, every one of which used to be refused
// with 'no match' because the match had already been deleted.
//
// So the match is held open instead. It is decided at once - the winner and the scoreline never
// change again - but it does not SETTLE until either every player on the roster has reported
// since the end, or this many seconds pass. Whichever comes first, so a player who has gone quiet
// can never hold a lobby open.
//
// The ordering this protects is the hub's, and it was already right (competitive.py: "we do not
// close the game and then record the match, we record the match and then close the game"). The
// close is armed by `match_result`, so holding that back until collection is done means the game
// is closed AFTER the data is in rather than during it.
// `??` does NOT catch NaN, only null/undefined, so `Number(undefined) ?? 12` is NaN and setTimeout
// silently fires on the next tick. Zero has to stay meaningful (it turns the window off), so the
// env var is checked explicitly rather than leaned on with `||`.
const COLLECT_SECONDS = (() => {
  const n = Number(process.env.COMP_COLLECT_SECONDS);
  return Number.isFinite(n) && n >= 0 ? n : 12;
})();
// HOW LONG THE START WAITS FOR THE GAME'S TEAMS TO MATCH THE LOBBY'S (Sam, 2026-09-15: "lets
// implement logic that the game also only starts if the teams in-game match the team distribution
// in the hub"). Everyone being in the match world is no longer enough on its own: the gamemode
// sweeps its roster and reports each player's in-game TeamID, and the match does not go `live`
// until those line up with the split the matchmaker chose.
//
// IT IS BOUNDED, AND THE BOUND IS THE WHOLE DESIGN. A gate that can hold ten players for ever is
// worse than teams being wrong - the players cannot see why nothing is happening and cannot fix it
// from inside the game. So an unsatisfied gate holds for this long and then starts anyway, LOUDLY:
// every client is sent match_teams_mismatch naming the players who are on the wrong side, and the
// verdict is kept on the match. A mismatch anyone can see beats a match nobody can start.
//
// 0 disables the gate entirely and restores the old behaviour in one environment variable, which
// is the escape hatch if this ever misbehaves in production.
const teamsGateSetting = process.env.COMP_TEAMS_GATE_SECONDS;
const TEAMS_GATE_SECONDS = teamsGateSetting !== undefined && teamsGateSetting.trim() !== '' &&
  Number.isFinite(Number(teamsGateSetting)) ? Math.max(0, Number(teamsGateSetting)) : 60;

// What a map NAME may look like. A ban is checked against COMP_MAP_POOL below as well; this
// is the shape check for a map that arrives on the connect POST.
const MAP_NAME = /^[\w .:'-]{1,64}$/;

// First to this many rounds wins (Sam: "whichever team reaches the score limit first, wins").
// It is only a FALLBACK: the gamemode reports `GetScoreLimit()` with every score, which is the
// live value off `DA_BB5.ScoringConfig.ScoreLimit` and therefore always right even if the pack
// is re-cooked with a different one. 7 is what DA_BB5 ships (docs/bodybomb-5v5.md) and what
// this is set to, so the fallback agrees with reality on the day it is used.
//
// COMP_LO_SCORE_LIMIT is the name to set: "the score LIGHTS OUT decides a win at", as opposed to
// COMP_GAME_RULES_OVERRIDE, which is the score the GAME plays to (server.cjs). The two are
// different numbers on different machines and were both called "the score limit" until the names
// started costing time. COMP_SCORE_LIMIT is still read, second, in case it is already set.
const DEFAULT_SCORE_LIMIT = Math.max(1, Number(process.env.COMP_LO_SCORE_LIMIT)
                                        || Number(process.env.COMP_SCORE_LIMIT) || 7);

// ...unless this says otherwise. A TESTING switch, and the only way COMP_LO_SCORE_LIMIT can win:
// with it set, the limit the gamemode reports is IGNORED and every match is decided at
// COMP_LO_SCORE_LIMIT instead. That is what makes `COMP_LO_SCORE_LIMIT=2` on Railway end a
// Bodybomb test match after two rounds without anyone rebuilding a pak - the game still plays to
// its own 7, but the server calls it (and rates it, and closes the window) at 2.
//
// Off in production, where the pak's own number is the honest one: a forced limit BELOW what the
// players see lands the result while they are still playing, and one ABOVE it means the match
// never finishes at all.
//
// Read per report rather than at boot, so a test can flip it around a single score and so the
// switch can be turned off on a running service the moment it is doing something silly.
function forcedScoreLimit() {
  const on = process.env.COMP_LO_SCORE_LIMIT_FORCE ?? process.env.COMP_SCORE_LIMIT_FORCE;
  if (!/^(1|true|yes|on)$/i.test(String(on || '').trim())) return null;
  return Math.max(1, Number(process.env.COMP_LO_SCORE_LIMIT)
                     || Number(process.env.COMP_SCORE_LIMIT) || 7);
}

// The ranked veto pool, now that the coin flip and the veto are decided HERE, not per-client.
// It mirrors the hub's competitive_pool(DEFAULT_MAPS): the Bodybomb 5v5 maps minus the ones
// Competitive excludes (Paintball). The server owns it so all ten clients ban against the SAME
// list and agree on the map; a client's catalogue order can no longer make the vetos diverge.
// Overridable so the wire test can drive a shorter veto.
const COMP_MAP_POOL = (process.env.COMP_MAP_POOL
  || 'Airsoft,BombHouse,Hospital,Pool,Rome,Russian')
  .split(',').map((s) => s.trim()).filter((s) => MAP_NAME.test(s));

// Who bans FIRST so the ban-advantage team bans LAST (controls the final map). Pure parity of
// the number of bans (pool - 1); mirrors hub/competitive.py ban_first_team exactly. Getting it
// backwards hands the map to the wrong team, so the two implementations share one rule.
function banFirstTeam(poolSize, advantageTeam) {
  const bansTotal = Math.max(0, (Number(poolSize) || 0) - 1);
  const other = advantageTeam === 1 ? 2 : 1;
  return (bansTotal % 2 === 1) ? advantageTeam : other;
}

const HEARTBEAT_MS = 15000;          // SSE comment frames, so proxies keep the pipe open
const LEGACY_COMPETITION = {MATCH_SIZE,TEAM_SIZE,MAX_PARTY,GATED_MODE_ID,COMP_MAP_POOL,DEFAULT_SCORE_LIMIT};

function create({ whoami, bearer, sendJson: rawSendJson, badRequest, readBody, upstashCmd, prefix, analytics, tournament, accountDirectory, admitGameplay, activityGate,
                 collectSeconds, requiredVersions, profileOf, relayIssuer, expectedRules, rankedRules, privateSoloSteam='',
                 modeId, competitionGuard, socialPrefix, sharedNetworkRegistry, directoryPresence }) {
  const mode = rankedModes.modeOf(modeId);
  const duel = mode.id === 'BB1';
  socialPrefix = socialPrefix || prefix || 'hub:';
  prefix = rankedModes.rankedPrefix(prefix, mode.id);
  const MATCH_SIZE = duel ? mode.players : LEGACY_COMPETITION.MATCH_SIZE;
  const TEAM_SIZE = duel ? mode.teamSize : LEGACY_COMPETITION.TEAM_SIZE;
  const MAX_PARTY = duel ? mode.maxParty : LEGACY_COMPETITION.MAX_PARTY;
  const GATED_MODE_ID = duel ? mode.id : LEGACY_COMPETITION.GATED_MODE_ID;
  const COMP_MAP_POOL = duel ? [mode.fixedMap] : LEGACY_COMPETITION.COMP_MAP_POOL;
  const DEFAULT_SCORE_LIMIT = duel ? mode.scoreLimit : LEGACY_COMPETITION.DEFAULT_SCORE_LIMIT;
  const soloMatch=match=>Boolean(privateSoloSteam&&identity.validSteam(privateSoloSteam)&&match?.players?.length===1&&
    identity.gameFor(match,match.host)===privateSoloSteam&&match.players[0].player_id===match.host);
  const sendJson = (res, status, body) => rawSendJson(res, status, identity.wire(body));
  // How long this service holds a decided match open for its last stats (see COLLECT_SECONDS).
  // Overridable per service so a test can close the window at once, or hold it open and drive it.
  const collectFor = Number.isFinite(Number(collectSeconds)) ? Number(collectSeconds) : COLLECT_SECONDS;
  const rulesExpected = typeof expectedRules === 'function' ? expectedRules : () => duel
    ? rankedModes.rulesOf(mode.id) : ({ score_limit: DEFAULT_SCORE_LIMIT, max_rounds: 12 });
  // What the queue demands right now: { hub, mode }, or null. server.cjs reads it off the
  // catalogue it is serving; the unit tests and solo_flow build this module directly and pass
  // nothing, and NOTHING REQUIRED MEANS NO GATE - the same fail-open as an unreadable catalogue.
  const requireVersions = typeof requiredVersions === 'function' ? requiredVersions : () => null;
  // steamId -> { hub, mode, at }: what each signed-in hub last told us it is running. Written on
  // EVERY authenticated request, which is the only reason a party member's version is known
  // without asking for it - their open stream is a request too, and they never POST a thing.
  const versions = new Map();
  const recentDuels = new Map();
  const networkRegistry = sharedNetworkRegistry || new networkLib.Registry();
  const credentialIssuer = relayIssuer || relayLib.createCredentialIssuer();
  const signalLimiter = relayLib.createRateLimiter({limit:120,windowMs:60000,maxEntries:2048});
  const reportLimiter = relayLib.createRateLimiter({limit:60,windowMs:60000,maxEntries:2048});
  // clientId -> { res, steamId, persona, since }
  const clients = new Map();
  // steamId -> Set<clientId>   (one player may have the hub open twice)
  const bySteam = new Map();
  // THE QUEUE HOLDS PARTIES, NOT PLAYERS (Sam, 2026-09-15; C10/C12).
  //
  // It used to be a flat array of steamIds and match formation took the first ten, which meant
  // a five-stack queueing was five unrelated entries that could - and regularly would - be cut
  // down the middle into opposite teams. A party is one unit now: it enters together, leaves
  // together, and matchmaker.js never splits it across the two sides.
  //
  // Each entry: { key, members: [steamId, ...], code, joined }
  //   key     `party:<code>` or `solo:<steamId>` - stable, and what the matchmaker reports back
  //   joined  the timestamp the WIDENING TOLERANCE is measured from. It is a plain clock
  //           reading and not always the real join time: a player sent back to the front of
  //           the queue after somebody else's dodge is given an older one, which is how
  //           "front of the queue" survives a queue that is no longer an ordered list.
  const queue = [];
  const queueIntents = new Map();   // caller -> pending admission identity
  const queueOf = new Map();         // steamId -> unit, the reverse index
  const matches = new identity.MatchMap();         // matchId -> match
  const inMatch = new Map();         // steamId -> matchId
  // Live-service keys are persistent player IDs; native adapters alone take Steam IDs.
  const verifiedPlayers = new Map();
  const gameOfPlayer = id => verifiedPlayers.get(id)?.game_steam_id ||
    (identity.validSteam(id) ? id : ''); // historical in-process queue entries
  function bindingConflict(members) {
    const games = members.map(gameOfPlayer);
    if (games.some(g => !identity.validSteam(g)) || new Set(games).size !== games.length) return true;
    const wanted = new Set(games), own = new Set(members);
    return [...queueOf.keys(), ...inMatch.keys()].some(id => !own.has(id) && wanted.has(
      identity.gameFor(matches.get(inMatch.get(id)), id) || gameOfPlayer(id)));
  }
  // steamId -> { rating, rd, vol, matches, wins, losses, updated }. Exactly the penalties
  // arrangement and for exactly the same reason: memory is the truth for the life of the
  // process, Upstash is the copy that survives a redeploy. A rank that evaporates on every
  // deploy is not a rank. See rating.cjs for what the numbers mean.
  const ratings = new Map();
  const ratingLoaded = new Set();    // steamIds we have already gone to Upstash for
  const ratingLoading = new Map();  // concurrent readers share the same pending read
  const ratingWrites = new Map();
  const ratingWriting = new Map();
  const ratingOperations = new Map();
  const matchWriting = new Map();
  // steamId -> { until, reason, count, elo }. Memory is the source of truth for the life of
  // the process; Upstash is the copy that survives a redeploy.
  const penalties = new Map();
  const penaltyWrites = new Map();
  const penaltyWriting = new Map();
  const combatHistories = new Map();
  const penaltyLoaded = new Set();   // steamIds we have already gone to Upstash for
  const penaltyLoading = new Map();
  // steamId -> [compact row, ...] newest first. The fallback when Upstash is not configured
  // (local runs and the test suite), and never the source of truth when it is.
  const history = new Map();
  const archived = new Map();        // matchId -> full record, same fallback
  // code -> { code, leaderId, members: [steamId, ...], created }. A party is ephemeral live
  // state, exactly like the queue and a match: it dies on redeploy and that is fine (Non-goals).
  // steamId -> [{at, by, reason, match_id, note}] newest first. Keyed by the REPORTED player,
  // because every question anyone asks of this is about them: how many, what for, by how many
  // different people. Keyed by reporter it would need a scan to answer any of those.
  // steamId -> Set(steamId). Three of them: accepted, incoming requests, outgoing requests.
  // Loaded lazily and written through, exactly like ratings and penalties.
  const friends = new Map();
  const reqIn = new Map();
  const reqOut = new Map();
  const friendsLoaded = new Set();
  const friendCodes = new Map();        // steamId -> code
  const codeOwners = new Map();         // code -> steamId

  // steamId -> {at, by, reason, until, note}. An ACCOUNT ban, not a queue cooldown: a no-show
  // penalty keeps you out of the queue for an hour, this keeps the Steam account out of
  // competitive entirely. Persisted, because a ban that a redeploy forgets is not a ban.
  const bans = new Map();
  const bansLoaded = new Set();

  const reports = new Map();
  const reportsLoaded = new Set();
  const reportLoadTimes = new Map(), reportsUnavailable = new Set();
  let reportIndexRead = null;
  let reportIndexAt = 0, reportIndexAvailable = typeof upstashCmd !== 'function';

  // steamId -> directory record (blankCareer). The same arrangement as ratings and penalties:
  // memory is the truth for the life of the process, the roster hash is the copy that survives a
  // redeploy. `rosterRead` is the one HGETALL, memoised as a promise so two console requests in
  // flight together cannot both read a half-filled map.
  const careers = new Map();
  const frozenCareers = new Set();
  const careerWrites = new Map();
  let rosterRead = null;
  let rosterAvailable = false;
  let ladderAdopted = false;

  // [{at, by, persona, text, hub, mode}, ...] newest first - see BUG_TEXT_MAX for why this is one
  // list and not a map, which is also why `bugsLoaded` is a flag rather than a set.
  let bugs = [];
  let bugsLoaded = false;
  const bugCooldown = new Map();     // steamId -> when they last filed one (ms)

  const parties = new Map();
  const partyOf = new Map();         // steamId -> code, the reverse index
  // steamId -> timer. A member's last window closed; they are removed from their party only if
  // this fires before they reconnect. Cleared in handleStream on reconnect.
  const partyGrace = new Map();
  // invitee steamId -> Map(inviter steamId -> { code, at, expires, timer }).
  // IN MEMORY ONLY, deliberately: an invite points at a party code, parties are ephemeral live
  // state that dies with the process, and a persisted invite would come back after a redeploy
  // offering a seat in a party that no longer exists.
  const partyInvites = new Map();

  const store = typeof upstashCmd === 'function' ? upstashCmd : null;
  const restitution = require('./cheater-restitution.cjs').create({store,prefix,modeId:mode.id,
    tournamentLedger:mode.id===require('./tournament.cjs').EVENT.mode?require('./tournament.cjs').ledgerBase(socialPrefix):undefined,
    notifyRefund:async correction=>{
      if(!correction.refunds?.length)return;
      const cheaters=await Promise.all(correction.cheaters.map(async id=>{
        const game=correction.cheater_games?.[id];
        const name=game&&typeof profileOf==='function'
          ?String((await profileOf(game,{signal:AbortSignal.timeout(5000)}))?.persona||'').trim():'';
        // Keep delivery pending when Steam is unavailable. An account nickname is
        // not a substitute for the Steam identity that played this match.
        if(!name)throw Error('Refund Steam profile unavailable');
        return name.slice(0,80);
      }));
      for(const refund of correction.refunds){
        const result=await messages.notifyRefund({target:refund.player_id,mode:mode.id,
          match_id:correction.match_id,amount:refund.amount,cheaters});
        if(!result.ok)throw Error('Refund notice pending: '+result.error);
      }
    },
    withRatings:(ids,fn)=>withRatingLocks(ids,async()=>{await Promise.all(ids.map(drainRatingWrites));return fn();}),
    committed:async(rows,fresh)=>{
      for(const row of rows){
        // Never restore the old correction snapshot over a later match on replay.
        if(store)await readRating(row.player_id);else if(fresh)ratings.set(row.player_id,row.after);
        pushRating(row.player_id,ratingOf(row.player_id));
      }
      refreshReaperCut(true);
    }});
  const restitutionTick=setInterval(()=>{restitution.run().catch(()=>{});},15000);
  restitutionTick.unref?.();
  const messages = require('./messages.cjs').create({store,prefix:socialPrefix,
    nudge:id=>sendTo(id,{type:'message_update'}),
    nameOf:async id=>{await loadProfile(id);return personaOf(id);}});
  const penaltyKey = (steamId) => `${prefix || 'hub:'}penalty:${steamId}`;
  const ratingKey = (steamId) => `${prefix || 'hub:'}rating:${steamId}`;
  const reportKey = (steamId) => `${prefix || 'hub:'}reports:${steamId}`;
  const bugsKey = () => `${prefix || 'hub:'}bugs`;
  // THE LEADERBOARD, ordered by the VISIBLE ladder (RR), not by matchmaking rating.
  //
  // A NEW KEY, deliberately. The old `leaderboard` set is scored in rating points and this one is
  // scored in RR; writing the new score into the old set would leave the two scales mixed for
  // every player who had not played since the deploy, and a board that is quietly mis-ordered is
  // worse than one that is visibly filling up. The old key is simply abandoned - nothing reads it,
  // and it costs one unused sorted set.
  const boardKey = () => `${prefix || 'hub:'}leaderboard:rr`;
  const banKey = (steamId) => `${prefix || 'hub:'}ban:${steamId}`;
  const friendsKey = (steamId) => `${socialPrefix}friends:${steamId}`;
  const profileKey = (steamId) => `${socialPrefix}profile:${steamId}`;
  const reqInKey = (steamId) => `${socialPrefix}friendreq:in:${steamId}`;
  const reqOutKey = (steamId) => `${socialPrefix}friendreq:out:${steamId}`;
  const codeKey = (steamId) => `${socialPrefix}friendcode:${steamId}`;
  const codeOwnerKey = (code) => `${socialPrefix}friendcodeowner:${code}`;
  const historyKey = (steamId) => `${prefix || 'hub:'}history:${steamId}`;
  const matchKey = (matchId) => `${prefix || 'hub:'}match:${matchId}`;
  const chatKey = (matchId) => `${prefix || 'hub:'}chat:${matchId}`;
  // The LIVE copy of a match in flight, and the index of them. Deliberately not matchKey's
  // namespace: that one is the finished record history reads. See LIVE_STATE_TTL_SECONDS.
  const liveMatchKey = (matchId) => `${prefix || 'hub:'}live:match:${matchId}`;
  const authorityKey = (id) => `${prefix || 'hub:'}live:authority:${id}`;
  const liveIndexKey = () => `${prefix || 'hub:'}live:matches`;
  const settlementKey = (id) => `${prefix || 'hub:'}settlement:${id}`;
  // THE DIRECTORY, one hash with one field per player. No TTL, for personaKey's reason: the
  // account the console most needs to look up is the one nobody has seen in a month.
  const rosterKey = () => `${prefix || 'hub:'}roster`;

  // ---------------------------------------------------------------- plumbing
  function send(clientId, event) {
    const client = clients.get(clientId);
    if (!client) return;
    try {
      client.res.write(`data: ${JSON.stringify(identity.wire(event))}\n\n`);
    } catch {
      drop(clientId);
    }
  }

  function sendTo(steamId, event) {
    for (const clientId of bySteam.get(steamId) || []) send(clientId, event);
  }

  function broadcast(event) {
    for (const clientId of clients.keys()) send(clientId, event);
  }

  function stats() {
    const inventory=accountDirectory?.snapshot();
    const registered=inventory?.players_registered;
    // `queued` stays a count of PEOPLE, not of units. The hub renders it as "n searching" and
    // a five-stack is five people searching; reporting 1 there would be a lie that happens to
    // match our new data structure.
    // `live_matches` is the whole in-flight registry - the same set the admin console calls
    // "live matches" - and NOT a filter on `state`. Every add to and every removal from
    // `matches` is already followed by a broadcast(stats()) (createMatch, closeMatch, settle,
    // leave, a dropped stream), whereas the transitions WITHIN a match - found -> ready ->
    // connecting -> live - are not. Counting a subset of states would therefore show a figure
    // that is minutes stale; counting the registry shows one that is never wrong.
    return { type: 'stats', online: bySteam.size, queued: queuedPlayers(),
             live_matches: matches.size, match_size: MATCH_SIZE,
             players_registered:inventory?.available && !inventory.stale && Number.isSafeInteger(registered) && registered>=0
               ? registered : null };
  }

  /**
   * Record what this request says it is running, off two headers the hub puts on everything
   * it sends (hub/live.py `_stamp`).
   *
   * HEADERS AND NOT A BODY, because the request that matters most for a party is the STREAM -
   * a GET with no body at all - and a member who is not the leader never sends anything else.
   */
  function noteVersions(req, steamId) {
    const h = (req && req.headers) || {};
    const hub = String(h['x-hub-version'] || '').trim().slice(0, 32);
    const mode = String(h['x-mode-version'] || '').trim().slice(0, 32);
    if (!hub && !mode) return;                // an old hub says nothing; leave what we had
    versions.set(steamId, { hub, mode, at: Date.now() });
  }

  /**
   * null when this player may queue, else what is out of date and what it has to be.
   *
   * `what` is 'hub', 'mode' or 'both', because the hub shows a different sentence (and a
   * different button) for each: one is an installer, the other is the gamemode's Update.
   */
  function versionProblem(steamId) {
    if (!VERSION_GATE) return null;
    let need = null;
    try { need = requireVersions(); } catch { need = null; }
    if (!need) return null;
    const have = versions.get(steamId);
    // A hub that has told us nothing: refused only in strict mode. See GATE_MODE for why that
    // rejects missing client versions.
    if (!have || (!have.hub && !have.mode)) {
      if (GATE_MODE !== 'strict') return null;
      return { what: 'both', mode_id: GATED_MODE_ID,
               need_hub: String(need.hub || ''), need_mode: String(need.mode || ''),
               have_hub: '', have_mode: '' };
    }
    const hubStale = versionOlder(have.hub, need.hub);
    // A hub that reports a hub version but NO mode version has no ranked pack installed. This
    // used to be waved through on the reasoning that "the tab cannot reach the queue without
    // one" - which was true of the Tk tab, whose gate screen replaces the Find match button, and
    // false of the web UI, which had no such gate. A player who installed Lights Out and opened
    // Competitive before installing anything queued for a mode they did not own (Sam's friend,
    // 2026-09-16). Both halves are fixed: the hub gates its own button again, and this refuses
    // the join - which is also the only layer that covers the OTHER members of a party.
    //
    // The two headers ship in the same release (hub/live.py `_stamp`), so a hub that can name
    // itself can name its pack: hub present + mode absent means not installed, not old client.
    const missing = Boolean(have.hub) && !have.mode;
    const modeStale = missing || (Boolean(have.mode) && versionOlder(have.mode, need.mode));
    if (!hubStale && !modeStale) return null;
    return {
      what: hubStale && modeStale ? 'both' : (hubStale ? 'hub' : 'mode'),
      mode_id: GATED_MODE_ID,
      // Not out of date, absent. The hub words that differently ("install" rather than
      // "update"); an older hub that does not know the flag still reads the rest and tells them
      // to go to Gamemodes, which is the right screen either way.
      missing_mode: missing,
      need_hub: String(need.hub || ''),
      need_mode: String(need.mode || ''),
      have_hub: have.hub || '',
      have_mode: have.mode || '',
    };
  }

  function drop(clientId) {
    const client = clients.get(clientId);
    if (!client) return;
    clients.delete(clientId);
    const set = bySteam.get(client.steamId);
    if (set) {
      set.delete(clientId);
      if (!set.size) {
        bySteam.delete(client.steamId);
        // Their reported version goes with them. Nothing reads it while they are away: the
        // caller's own arrives on the join POST, and a party member has to be connected
        // (the `away` check) before anybody can queue them.
        versions.delete(client.steamId);
        // the player's last window closed: they leave the queue, but a match they have
        // already been put into is NOT dissolved here - abandoning is the penalty ladder's
        // business (memory section 2, C6-C9), not a side effect of closing a window.
        //
        // Their PARTY leaves the queue with them, and the survivors are told. A member who has
        // closed the hub cannot press Accept, so a party still searching with a hole in it is
        // searching for a match it is guaranteed to cancel twenty seconds later. Better to stop
        // and let them re-queue than to spend somebody else's accept window on it.
        const wasQueued = removeFromQueue(client.steamId);
        if (wasQueued) {
          for (const id of wasQueued.members) {
            if (id !== client.steamId) sendTo(id, { type: 'unqueued', reason: 'member_left' });
          }
        }
        // ...and a party they are in is NOT dissolved here either. A brief stream drop must be a
        // no-op (handleStream cancels this on reconnect); only if the grace runs out without them
        // coming back are they removed and the survivors told.
        if (partyOf.has(client.steamId) && !partyGrace.has(client.steamId)) {
          const steamId = client.steamId;
          const timer = setTimeout(() => {
            partyGrace.delete(steamId);
            if (!bySteam.has(steamId) && partyOf.has(steamId)) {
              leaveParty(steamId);
              sendTo(steamId, { type: 'party_update', code: null });  // harmless if truly gone
            }
          }, PARTY_GRACE_SECONDS * 1000);
          if (typeof timer.unref === 'function') timer.unref();
          partyGrace.set(steamId, timer);
        }
      }
    }
    try { client.res.end(); } catch { /* already gone */ }
    broadcast(stats());
  }

  // ---------------------------------------------------------------- the queue
  /** Every steamId currently queueing, across all units. */
  function queuedPlayers() {
    let n = 0;
    for (const unit of queue) n += unit.members.length;
    return n;
  }

  /** Where a player's unit sits, 1-based, or 0 when they are not queueing. Kept for the
   *  `queued` event, which the hub shows as "position". Units, not players: with parties in
   *  the queue "you are 7th" was never a count of people ahead of you anyway. */
  function queuePosition(steamId) {
    const unit = queueOf.get(steamId);
    return unit ? queue.indexOf(unit) + 1 : 0;
  }

  function isQueued(steamId) {
    return queueOf.has(steamId);
  }

  /**
   * Put a unit in the queue. `joined` is the clock the widening tolerance runs from, so
   * handing in an older one is how a player is sent back to the FRONT: the matchmaker sorts
   * on it, anchors on the oldest, and gives it the widest window. There is no unshift any
   * more because the queue is no longer a line - it is a pool with a wait clock each.
   */
  function enqueue(members, code, joined) {
    const ids = [...new Set(members.filter(Boolean))];
    if (duel && ids.length !== 1) return null;
    if (competitionGuard && !competitionGuard.canEnter(mode.id, ids, ids.map(gameOfPlayer))) return null;
    if (!ids.length || ids.some(id => inMatch.has(id) || queueOf.has(id))) return null;
    const unit = {
      key: code ? `party:${code}` : `solo:${ids[0]}`,
      members: ids,
      code: code || '',
      joined: Number(joined) || Date.now(),
    };
    queue.push(unit);
    for (const id of ids) queueOf.set(id, unit);
    return unit;
  }

  /**
   * Take the player's whole unit out of the queue and return it, or null.
   *
   * THE WHOLE UNIT, always. A party queues as one thing, so it stops queueing as one thing:
   * leaving three of a five-stack in a queue they cannot fill a team with, waiting on a leader
   * who has closed the hub, is not a state anybody should be able to reach. The caller decides
   * who gets told - `unqueued` goes to every member.
   */
  function removeFromQueue(steamId) {
    const unit = queueOf.get(steamId);
    if (!unit) return null;
    const i = queue.indexOf(unit);
    if (i >= 0) queue.splice(i, 1);
    for (const id of unit.members) {
      queueOf.delete(id);
      networkRegistry.retirePlayer(id);
    }
    return unit;
  }

  // ---------------------------------------------------------------- ratings
  /** What we currently believe about a player. Never null, never blocks: an unknown player is
   *  a default record (1500 / RD 350), which is the honest thing to say about them. */
  function ratingOf(steamId) {
    return ratingLib.normalise(ratings.get(steamId));
  }

  /**
   * WHAT THE BOARD IS ORDERED BY: the player's RR total, which is the ladder they can see.
   *
   * It used to be the Glicko-2 rating - so the board was ordered by a number no player is ever
   * shown, and could seat somebody above a player whose visible rank was higher. Valorant's
   * leaderboard is RR, and so is this. RR keeps counting past the capstone, so Reaper orders
   * itself by how far past it a player is, which is the behaviour that board exists for.
   */
  function boardScore(record) {
    return Math.max(0, Math.floor(Number(record && record.progress) || 0));
  }

  // ---------------------------------------------------------------- the capstone cut
  //
  // THE TOP RANK IS A SEAT ON THE BOARD, NOT A BAND OF THE LADDER. progress.cjs can say a player
  // has the RR to be eligible (REAPER_AT) and nothing more: whether they actually hold the
  // capstone depends on the REAPER_SLOTS highest RR totals in the world, which is a leaderboard
  // question and therefore this module's. Sam, 2026-09-16: "once a user hits 300 RR, they become
  // eligible to become reaper if their RR is in the top 150 highest RR values on the leaderboard".
  //
  // ONE CACHED READ, NOT A QUERY PER PLAYER. The badge is drawn on every rank event - `hello`,
  // every settled match, every leaderboard row - and asking the store where a player sits each
  // time would put a network round trip inside the hot path of a match result. The cut moves when
  // somebody plays, so a stale one is wrong only about players sitting within a match's RR of the
  // line, and only until the next refresh.
  const REAPER_AT = progressLib.REAPER_AT;
  const REAPER_SLOTS = progressLib.REAPER_SLOTS;
  const REAPER_REFRESH_MS = Math.max(5000,
    Number(process.env.COMP_REAPER_REFRESH_MS) || 60000);
  // `ids`: who is seated as of the last read. `cut`: the lowest seated score. `full`: whether
  // all the seats are taken - when they are not, eligibility IS the capstone, which is also what
  // happens on a service with no store at all (tests, local runs): there is no board to be in the
  // top 150 of, so refusing everyone would be inventing a rule nobody can satisfy.
  let reaperCut = { ids: new Set(), cut: REAPER_AT, full: false, at: 0 };
  let reaperBusy = false;

  /** Is this player seated at the capstone right now? Synchronous, off the cached cut. */
  function isReaper(steamId, progress) {
    const rr = Math.max(0, Math.floor(Number(progress) || 0));
    if (rr < REAPER_AT) return false;                    // not eligible; nothing else matters
    if (reaperCut.ids.has(String(steamId))) return true;
    // Above the lowest seated score but not in the snapshot: they climbed past somebody since
    // the last read, so they have the seat and the snapshot is what is out of date.
    return reaperCut.full ? rr > reaperCut.cut : true;
  }

  /**
   * Re-read the cut, at most once every REAPER_REFRESH_MS. Fire and forget - a failed read
   * leaves the previous cut standing, which is a slightly stale badge rather than a rank that
   * vanishes off every top player's hub because one request timed out.
   */
  function refreshReaperCut(force = false) {
    if (!store || reaperBusy) return Promise.resolve(reaperCut);
    if (!force && Date.now() - reaperCut.at < REAPER_REFRESH_MS) return Promise.resolve(reaperCut);
    reaperBusy = true;
    // WITHSCORES, which Upstash returns as a flat [member, score, member, score, ...]. The rest
    // of this file reads scores off the ratings it keeps per player instead, because those rows
    // are ones it has loaded anyway; the players at the top of the board are exactly the ones
    // this process may never have seen.
    return Promise.resolve(store(['ZREVRANGE', boardKey(), '0', String(REAPER_SLOTS - 1),
                                  'WITHSCORES']))
      .then((raw) => {
        const flat = Array.isArray(raw) ? raw : [];
        const seated = [];
        for (let i = 0; i + 1 < flat.length; i += 2) {
          const score = Math.floor(Number(flat[i + 1]) || 0);
          if (score >= REAPER_AT) seated.push({ id: String(flat[i]), score });
        }
        const full = seated.length >= REAPER_SLOTS;
        reaperCut = {
          ids: new Set(seated.map((s) => s.id)),
          cut: full ? seated[seated.length - 1].score : REAPER_AT,
          full,
          at: Date.now(),
        };
        return reaperCut;
      })
      .catch(() => {
        // A FAILED READ STILL COUNTS AS A READ. Without stamping the clock, every `hello` and
        // every settled match would fire another request at a store that is already failing -
        // and the previous cut, which is what we fall back to, is no fresher for the traffic.
        reaperCut = { ...reaperCut, at: Date.now() };
        return reaperCut;
      })
      .then((cut) => { reaperBusy = false; return cut; });
  }

  async function loadRating(steamId) {
    if (ratingLoaded.has(steamId) || !store) return ratingOf(steamId);
    if (ratingLoading.has(steamId)) return ratingLoading.get(steamId);
    const pending = readRating(steamId);
    ratingLoading.set(steamId, pending);
    try { return await pending; }
    finally { ratingLoading.delete(steamId); }
  }

  async function readRating(steamId, mirror = true) {
    // A failed read is unknown, not a new player. Leave it retryable and do not emit
    // an unranked event or admit the player to matchmaking with a default record.
    const raw = await store(['GET', ratingKey(steamId)], { strict: true });
    if (raw) {
      const parsed = typeof raw === 'string' ? identity.parse(raw) : identity.hydrate(structuredClone(raw));
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('Invalid saved rating');
      }
      const known = ratings.get(steamId);
      // A rating written in THIS process while the read was in flight is newer and wins -
      // the same rule loadPenalty uses, and for the same reason.
      if (!known || (parsed.revision || 0) > (known.revision || 0)) {
        ratings.set(steamId, ratingLib.normalise(parsed));
      }
    }
    ratingLoaded.add(steamId);
    const loaded = ratingOf(steamId);
    // ...and into the directory, which is how the console shows a rank for somebody who has not
    // signed in since this process started.
    if (mirror) noteRating(steamId, loaded);
    return loaded;
  }

  /** Every one of them, in parallel. Awaited before a unit is allowed into the queue, so the
   *  matchmaker never sorts on a default rating for somebody we actually know. */
  function loadRatings(steamIds) {
    return Promise.all([...new Set(steamIds)].map((id) => loadRating(id)));
  }

  // Reserve all participants synchronously before awaiting any work. Every rank
  // read-modify-write shares this queue, including settlement and conduct costs.
  function withRatingLocks(ids, action) {
    const unique = [...new Set(ids)];
    const before = unique.map(id => ratingOperations.get(id) || Promise.resolve());
    const pending = Promise.all(before.map(p => p.catch(() => {}))).then(action);
    for (const id of unique) ratingOperations.set(id, pending);
    pending.finally(() => {
      for (const id of unique) if (ratingOperations.get(id) === pending) ratingOperations.delete(id);
    }).catch(() => {});
    return pending;
  }

  function mutateRating(steamId, change, audit = null) {
    return withRatingLocks([steamId], async () => {
      await drainRatingWrites(steamId);
      const before = await loadRating(steamId);
      const next = change(before);
      if (!next) return before;
      saveRating(steamId, next, change, audit);
      await drainRatingWrites(steamId);
      return ratingOf(steamId);
    });
  }

  function saveRating(steamId, record, change = null, audit = null) {
    const clean = ratingLib.normalise(record);
    const previous = ratingOf(steamId);
    clean.revision = (Number(previous.revision) || 0) + 1;
    if(!audit || !store){ratings.set(steamId, clean);ratingLoaded.add(steamId);}
    // The admin directory mirrors wins, losses, MMR and RR so a thousand-row table is not a
    // thousand reads. Written here, where the number actually changes, and read nowhere else.
    if(!audit || !store)noteRating(steamId, clean);
    if (!store) return clean;
    // No TTL. A penalty is meant to expire; a rank is not, and a player who takes three months
    // off should come back to their level rather than to a fresh account.
    const writes = ratingWrites.get(steamId) || [];
    const operation=change ? crypto.randomBytes(16).toString('hex') : null;
    writes.push({ id: steamId, expected: clean.revision - 1, json: JSON.stringify(clean),
                  progress: boardScore(clean), placing: ratingLib.isPlacing(clean), change,
                  operation, audit_context:audit, audit_at:Date.now(),
                  audit:audit ? JSON.stringify({id:operation,at:Date.now(),...audit,target_id:steamId,before:previous,after:clean}) : null });
    ratingWrites.set(steamId, writes);
    drainRatingWrites(steamId).catch(() => { /* retained for the next strict retry */ });

    // THE LEADERBOARD INDEX. Ratings are one key per player, which answers "what is this player's
    // rating" and nothing else - there is no way to ask "who are the top fifty" without scanning
    // every key in the database. A sorted set costs one extra write here and makes that question a
    // single range query.
    //
    // PLACING PLAYERS ARE NOT ON IT. Their rating is real and the matchmaker uses it, but a board
    // built from ratings we are not yet confident enough to show as a rank would seat a player
    // above people who have played fifty matches on the strength of one good night. They join the
    // board on the same event that gives them a rank.
    return clean;
  }

  function drainRatingWrites(steamId) {
    if (!store) return Promise.resolve();
    if (ratingWriting.has(steamId)) return ratingWriting.get(steamId);
    const pending = Promise.resolve().then(async () => {
      const writes = ratingWrites.get(steamId) || [];
      while (writes.length) {
        const row = writes[0];
        for (let attempt = 0; ; attempt++) {
          try {
            const keys = [ratingKey(steamId), boardKey()];
            if (row.operation) keys.push(`${prefix}rating-operation:${row.operation}`);
            if (row.audit) keys.push(`${prefix}analytics:audit:${row.operation}`,`${prefix}analytics:audits`);
            const committed = await settlementLib.write(store, keys, row);
            if (committed && writes.length === 1) {
              ratings.set(steamId, ratingLib.normalise(committed));
              noteRating(steamId, committed);
            }
            break;
          } catch (error) {
            if (!error.conflict || !row.change || attempt >= 2) throw error;
            // Preserve the pending operation across network failure and a
            // different container's update. Its receipt prevents double charge
            // if an earlier request succeeded but its response was lost.
            const raw = await store(['GET', ratingKey(steamId)], { strict: true });
            const current = ratingLib.normalise(raw ? identity.parse(raw) : ratingLib.defaultRating());
            const next = ratingLib.normalise(row.change(current) || current);
            next.revision = (current.revision || 0) + 1;
            Object.assign(row, { expected: current.revision || 0, json: JSON.stringify(next),
              progress: boardScore(next), placing: ratingLib.isPlacing(next) });
            if(row.audit_context)row.audit=JSON.stringify({id:row.operation,at:row.audit_at,...row.audit_context,target_id:steamId,before:current,after:next});
          }
        }
        writes.shift();
      }
      ratingWrites.delete(steamId);
    }).finally(() => { ratingWriting.delete(steamId); });
    ratingWriting.set(steamId, pending);
    return pending;
  }

  // ---------------------------------------------------------------- penalties
  function livePenalty(steamId) {
    if (queuePenaltiesPaused()) return null;
    const p = penalties.get(steamId);
    if (!p) return null;
    if (noShowPenaltiesPaused() && (!p.reason || p.reason === 'no_show')) return null;
    if (p.until <= Date.now()) {
      // The ban is served. The COUNT stays, because that is what the ladder escalates on and
      // what decays with clean time. Only the ban itself expires.
      if (!p.count) penalties.delete(steamId);
      else penalties.set(steamId, { ...p, until: 0 });
      return null;
    }
    return p;
  }

  /** The offence count after clean time has been taken off it. */
  function effectiveCount(record) {
    if (!record || !record.count) return 0;
    const since = record.last || 0;
    const clean = since ? Math.floor((Date.now() - since) / (NO_SHOW_DECAY_SECONDS * 1000)) : 0;
    return Math.max(0, record.count - clean);
  }

  function rungSeconds(count) {
    const i = Math.min(Math.max(1, count), NO_SHOW_LADDER.length) - 1;
    return NO_SHOW_BAN_SECONDS * NO_SHOW_LADDER[i];
  }

  /** What the player's NEXT no-show would cost, so the hub can warn them honestly. */
  function nextNoShowSeconds(steamId) {
    return rungSeconds(effectiveCount(penalties.get(steamId)) + 1);
  }

  async function loadPenalty(steamId) {
    if (!store) return livePenalty(steamId);
    await drainPenaltyWrites(steamId);
    if (penaltyLoading.has(steamId)) return penaltyLoading.get(steamId);
    const pending = (async () => {
      const beforeRead = penalties.get(steamId);
      const raw = await store(['GET', penaltyKey(steamId)], { strict: true });
      if (raw) {
        const parsed = identity.parse(raw);
        if (!parsed || typeof parsed !== 'object' || !Number.isFinite(Number(parsed.until))) {
          throw new Error('Invalid saved penalty');
        }
        const known = penalties.get(steamId);
        // Keep a concurrently applied local strike; otherwise use the current
        // durable record, including strikes committed by another service instance.
        if (known === beforeRead || !known || (parsed.last || 0) >= (known.last || 0)) penalties.set(steamId, parsed);
      } else if (penalties.get(steamId) === beforeRead) penalties.delete(steamId);
      penaltyLoaded.add(steamId);
      return livePenalty(steamId);
    })();
    penaltyLoading.set(steamId, pending);
    try { return await pending; }
    finally { penaltyLoading.delete(steamId); }
  }

  function savePenalty(steamId, record) {
    penalties.set(steamId, record);
    penaltyLoaded.add(steamId);
    if (!store) return;
    const pending = penaltyWrites.get(steamId) || [];
    pending.push(penaltyWriteLib.prepare(record));
    penaltyWrites.set(steamId, pending);
    drainPenaltyWrites(steamId).catch(() => { /* retained for a strict retry before queueing or combat */ });
  }

  function drainPenaltyWrites(id) {
    if (!store || !penaltyWrites.get(id)?.length) return Promise.resolve();
    if (penaltyWriting.has(id)) return penaltyWriting.get(id);
    const pending = (async () => {
      const writes = penaltyWrites.get(id);
      while (writes?.length) {
        const saved = await penaltyWriteLib.write(store, penaltyKey(id), writes[0]);
        if (writes.length === 1) {
          if (saved.record) penalties.set(id, saved.record);
          else penalties.delete(id);
        }
        writes.shift();
      }
      penaltyWrites.delete(id);
    })();
    penaltyWriting.set(id, pending);
    pending.finally(() => {
      if (penaltyWriting.get(id) === pending) penaltyWriting.delete(id);
    }).catch(() => {});
    return pending;
  }

  /**
   * Record a no-show. Returns what the player should be told.
   *
   * The points figure used to be RECORDED AND NOT APPLIED, because there was no rank service to
   * apply it to. Then it was applied to the matchmaking rating, which a player never sees. It is RR
   * now (progress.penalise): the ladder they watch, moved the moment the offence lands.
   *
   * It is `penalise`, not `update`: the player did not play, so it must not touch their RD, must
   * not count towards their placements, and must not teach the MATCHMAKER anything about how good
   * they are - which is exactly why the cost belongs on RR and not on MMR. The running total on
   * the penalty record is kept as well; it is what the history rows read to show what an offence
   * cost.
   */
  function applyNoShow(steamId) {
    if (noShowPenaltiesPaused()) return null;
    const previous = penalties.get(steamId) || { count: 0, elo: 0, last: 0 };
    const count = effectiveCount(previous) + 1;
    const seconds = rungSeconds(count);
    const record = {
      until: Date.now() + seconds * 1000,
      last: Date.now(),
      reason: 'no_show',
      count,
      // `elo` is the wire and storage name for the running total of POINTS lost to penalties -
      // history rows and every hub already in the field read it - and those points are RR now.
      // The name is kept; the meaning is the one the screens describe.
      elo: (previous.elo || 0) + NO_SHOW_RR,
    };
    savePenalty(steamId, record);
    if (NO_SHOW_RR) {
      // Load-then-charge, so a player whose record has not been read this process is charged
      // against their real RR rather than against a fresh zero. The ban is already applied above
      // and does not wait for this.
      mutateRating(steamId, (current) => {
          const hit = progressLib.penalise(current, NO_SHOW_RR);
          if (!hit.delta) return null;      // placing, or already on the floor
          // Only `progress` moves. rating, rd, vol, matches, wins and losses are the record of
          // matches PLAYED, and nothing about a match somebody skipped belongs in them.
          return { ...current, progress: hit.progress, demoteArmed: false };
        })
        .catch(() => { /* the ban still stands; the debt is on the penalty record either way */ });
    }
    return { seconds, until: record.until, reason: 'no_show',
             // Both names, one number: `elo` for hubs shipped before the swap, `rr` for the ones
             // that say RR on screen.
             elo: NO_SHOW_RR, rr: NO_SHOW_RR, count,
             next_seconds: rungSeconds(count + 1) };
  }

  // ---------------------------------------------------------------- history
  /** The team a player ended up on, or 0 when the veto never got that far. */
  function teamOf(match, steamId) {
    for (const key of ['1', '2']) {
      if ((match.teams && match.teams[key] || []).includes(steamId)) return Number(key);
    }
    return 0;
  }

  /**
   * One match, as the player who was in it should see it later.
   *
   * Deliberately small: this is what the list draws, and it is stored once per player, so
   * anything the detail pop-up can fetch on demand belongs in the full record instead.
   */
  function historyRow(match, record, steamId) {
    const roster = everyone(match);
    const me = roster.find((p) => p.player_id === steamId) || {};
    const team = teamOf(match, steamId);
    return {
      id: match.id,
      mode: match.mode || mode.id,
      ...(duel?{opponents:roster.filter(p=>p.player_id!==steamId).map(p=>p.player_id)}:{}),
      ended: record.ended,
      map: record.map || '',
      outcome: me.left ? 'cancelled' : record.outcome,
      // Walking out is this player's own offence, whatever became of the match afterwards.
      reason: me.left ? 'abandoned' : (record.reason || ''),
      blamed: (record.blame || []).includes(steamId) || Boolean(me.left),
      team,
      side: (team && record.sides && record.sides[String(team)]) || '',
      host: Boolean(record.host) && record.host === steamId,
      players: roster.length,
      // Unknown until the match settles. Written explicitly rather than left out so the hub
      // can tell "we do not know yet" from "this field is new"; patchResult fills them in.
      won: null,
      score: null,
      delta: null,
      rr_delta: null,
      placement: null,
      // THIS PLAYER'S OWN LINE, so the list can show a K/D without fetching the whole record for
      // all fifty rows. Null at archive time (the match is archived the moment it goes live, when
      // the sweep has reported nothing yet) and patched by patchResult / writeScoreboard.
      ...statLine(match, steamId),
      elo: (record.punished && record.punished[steamId]
            && typeof record.punished[steamId].elo === 'number')
        ? -record.punished[steamId].elo : null,
      connected: Boolean(me.connected),
      accepted: Boolean(me.accepted),
    };
  }

  /**
   * Everyone who was ever in this match: the current roster plus anyone who walked out of it.
   *
   * A leaver is taken off `match.players` so the accept and connect counts stay honest, but
   * leaving them out of the RECORD would mean the one thing the abandon ladder (C6-C9) most
   * needs to see is the one thing never written down.
   */
  function everyone(match) {
    const gone = (match.left || []).map((p) => ({ ...p, left: true }));
    return [...match.players.map((p) => ({ ...p, left: false })), ...gone];
  }

  // ---------------------------------------------------------------- the scoreboard
  /**
   * THE MATCH SCOREBOARD - per player, as the detail view draws it.
   *
   * Everything here was already being collected and then thrown away at the door: the stat sweep
   * (`gameReportedStats`) accumulates a row per player onto the LIVE match, and the kill feed
   * (`gameReportedKill`) accumulates every kill with both ends attributed. Neither reached the
   * ARCHIVE, so the moment a match ended its scoreboard ceased to exist and the hub had nothing
   * to draw but the line "per-player stats are not collected yet".
   *
   * WHAT EACH NUMBER MEANS, because two of them do not mean what their name suggests:
   *
   *   kills       `ABodycamPlayerState::Kill`, and it is a NET score - a team kill DECREMENTS it,
   *               measured 2026-09-15 (Sam killed one enemy and two team-mates and it read -1).
   *               It is shown because it is the number the game's own scoreboard shows, and it is
   *               signed for the same reason.
   *   team_kills  from the KILL FEED, not from the net counter: every kill whose killer and victim
   *               are both named and on the same side. On the match that settled this the feed
   *               found six where the netted counter implied two. `null` when no feed arrived at
   *               all - 0 would be a claim we cannot make - and a real 0 once one has.
   *   score       NOT included. `GetPlayerScore` was measured to track the player's TEAM round
   *               wins, identical for all five players on a side, so a "Score" column built on it
   *               would discriminate nobody while looking like it did.
   *   spawns      `SpawnCount` counts SPAWNS, not rounds. Kept as the diagnostic it is and never
   *               used as a denominator.
   *
   * HONEST NULLS THROUGHOUT. A field the pak never reported is `null`, not 0: a column of zeroes
   * reads as "everybody went 0-0", which is the one thing this must not say. A player the sweep
   * never reached still gets a row (so the board matches the two teams) with `reported: false`.
   *
   * Returns [] when nothing at all is known, which is what a match that ended before the gamemode
   * said anything looks like, and is how the hub decides whether to draw a board at all.
   */
  function scoreboardOf(match) { return identity.wire(buildScoreboard(match)); }
  function buildScoreboard(match) {
    try {
      const rows = (match && match.stats && Array.isArray(match.stats.players))
        ? match.stats.players : [];
      const feed = Array.isArray(match && match.kills) ? match.kills : [];
      if (!rows.length && !feed.length && !match.combatState) return [];

      // Only a kill with BOTH ends named is counted, exactly as the enforcement counts it: an
      // inference from alive counts is a number, not a person, and must not appear beside a name.
      const teamKills = new Map();
      for (const k of feed) {
        if (!k || !k.teamKill || !k.killer || !k.victim) continue;
        teamKills.set(k.killer, (teamKills.get(k.killer) || 0) + 1);
      }
      const byId = new Map();
      for (const r of rows) if (r && r.steamId) byId.set(r.steamId, r);

      const num = (v) => (Number.isFinite(v) ? v : null);
      const flag = (v) => (typeof v === 'boolean' ? v : null);
      return everyone(match).map((p) => {
        const r = byId.get(p.player_id) || {};
        return {
          player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id),
          team: teamOf(match, p.player_id),
          reported: byId.has(p.player_id),
          kills: num(r.kills),
          deaths: num(r.deaths),
          // 0 is real once a feed exists; null means no feed reached us for this match.
          team_kills: feed.length ? (teamKills.get(p.player_id) || 0) : null,
          rounds_won: num(r.roundsWon),
          clutches: num(r.clutches),
          ping: num(r.ping),
          spawns: num(r.spawnCount),
          late_join: flag(r.lateJoin),
          spectator: flag(r.spectator),
          ...(r.disconnected === true || p.left === true ? {disconnected:true} : {}),
          ...(r.stats_complete === false ? {stats_complete:false} : {}),
          ...(match.combatState ? { combat: combatSummary(match, p.player_id, combatRounds(match, r)) } : {}),
        };
      });
    } catch {
      // A scoreboard that could not be built is a missing scoreboard, never a failed archive.
      return [];
    }
  }

  /** One player's own line, for their history ROW - the K/D the list shows without opening it. */
  function combatRounds(match, row = {}) {
    const candidates = [row.roundsPlayed, match.stats?.rounds,
      Number(match.score?.[1] || 0) + Number(match.score?.[2] || 0)];
    return candidates.find(value => Number.isFinite(value) && value > 0) || null;
  }

  function combatStats(match) {
    const stats = {...(match.stats || {}), ...(match.start_ready_verified ? {round_index_base:0} : {})};
    if (!match.combatState) return stats;
    const existing = new Map((stats.players || []).map(row => [row.steamId, row]));
    return { ...stats, players: everyone(match).map(player => {
      const row = existing.get(player.player_id) || { steamId: player.player_id };
      return { ...row, combat: combatSummary(match, player.player_id, combatRounds(match, row)) };
    }) };
  }

  function statLine(match, steamId) {
    const board = scoreboardOf(match);
    const mine = board.find((r) => r.player_id === steamId);
    if (!mine) return { kills: null, deaths: null, team_kills: null };
    return { kills: mine.kills, deaths: mine.deaths, team_kills: mine.team_kills };
  }

  /** The whole match, for the detail pop-up. Fetched only when a row is clicked. */
  function fullRecord(match, record) { return identity.wire(buildFullRecord(match, record)); }
  function buildFullRecord(match, record) {
    const roster = everyone(match);
    const detailRounds = roundDetails(match);
    return {
      id: match.id,
      mode: match.mode || mode.id,
      created: match.created,
      started: match.live_at || null,
      initial_host:match.initial_host||null,starting_sides:match.starting_sides||null,
      ended: record.ended,
      outcome: record.outcome,
      reason: record.reason || '',
      map: record.map || '',
      host: record.host || '',
      game_bindings: match.game_bindings,
      host_game_steam_id: identity.gameFor(match, record.host || match.host),
      size: roster.length,
      // The coin flip, the side choice and the veto run on the SERVER now, so a normal match's
      // teams/sides/bans are server-decided ('server'). A match that came in on the old path -
      // teams reported with the connect POST - is still marked 'client' so a later reader knows
      // it was only as trustworthy as the hub that sent it.
      source: match.lobby_server ? 'server' : (match.lobby_reported ? 'client' : 'server'),
      teams: match.teams || {},
      sides: match.sides || {},
      bans: match.bans || [],
      // How the score got to where it got. [] for a match that never reported one, which is
      // different from a match with no rounds and is why it is always present.
      rounds: Array.isArray(match.rounds) ? match.rounds.slice() : [],
      round_details: detailRounds,
      round_index_base: 0,
      // Every team kill the match saw, with its verdict. The admin console reads this; it is also
      // the evidence for turning enforcement on, since it says what WOULD have been punished.
      teamkills: Array.isArray(match.teamkills) ? match.teamkills.slice() : [],
      // THE TIMELINE. Every clock this match was put on and the close that ended it, in order.
      // It is what makes a cancellation self-explanatory a day later; see note().
      diag: Array.isArray(match.diag) ? match.diag.slice() : [],
      players: roster.map((p) => ({
        player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id),
        persona: p.persona || '',
        accepted: Boolean(p.accepted),
        connected: Boolean(p.connected),
        left: Boolean(p.left),
        team: teamOf(match, p.player_id),
        blamed: (record.blame || []).includes(p.player_id) || Boolean(p.left),
        elo: (record.punished && record.punished[p.player_id]
              && typeof record.punished[p.player_id].elo === 'number')
          ? -record.punished[p.player_id].elo : null,
      })),
      won_team: null,
      score: null,
      // THE SCOREBOARD. Empty at go-live, when this record is first written, and filled by
      // writeScoreboard once the sweep has said something. The display count uses
      // the same normalization as the round selector (native index zero is round one).
      scoreboard: scoreboardOf(match),
      rounds_played: detailRounds.length || null,
    };
  }

  /**
   * A match ended. Write it where the Match History sub-tab can find it.
   *
   * Fire and forget, exactly like savePenalty: a store that is down or absent must cost the
   * ten players nothing, and the in-memory copy still serves this process.
   */
  function archiveMatch(match, options = {}) {
    // Wrapped whole. This runs in the middle of ending a match for ten people, and NOTHING
    // about writing history may cost them the exit: an archive that fails is a missing row,
    // never a match left pinned in `inMatch` with no timer.
    try {
      const { outcome, reason = '', blame = [], punished = {} } = options;
      if (!match || match.archived) return null;
      const record = {
        ended: Date.now(), outcome, reason, blame, punished,
        map: match.map || '', host: match.host || '', sides: match.sides || {},
      };
      const full = fullRecord(match, record);
      // Only now: a record that could not be built must stay archivable by a later, healthier
      // call rather than burning the flag on the way past.
      match.archived = true;

      archived.set(match.id, full);
      if (archived.size > ARCHIVE_MEMORY_KEEP) {
        // Map keeps insertion order, so the oldest key is the first one out.
        archived.delete(archived.keys().next().value);
      }
      // Built once and used for both copies, so the memory fallback and the stored row can
      // never disagree about the same match.
      const rows = [];
      for (const p of everyone(match)) {
        try { rows.push([p.player_id, historyRow(match, record, p.player_id)]); } catch { /* skip */ }
      }
      for (const [steamId, row] of rows) {
        const list = history.get(steamId) || [];
        list.unshift(row);
        history.set(steamId, list.slice(0, HISTORY_KEEP));
      }
      if (!store) return full;

      const writes = [store(['SET', matchKey(match.id), JSON.stringify(full),
                             'EX', String(MATCH_TTL_SECONDS)])];
      for (const [steamId, row] of rows) {
        const key = historyKey(steamId);
        writes.push(Promise.resolve(store(['LPUSH', key, JSON.stringify(row)]))
          .then(() => store(['LTRIM', key, '0', String(HISTORY_KEEP - 1)]))
          .then(() => store(['EXPIRE', key, String(HISTORY_TTL_SECONDS)])));
      }
      Promise.all(writes).catch(() => { /* the in-memory copy still answers this process */ });
      return full;
    } catch {
      return null;
    }
  }

  /**
   * Put this match's scoreboard onto the record that was archived at go-live.
   *
   * `patchResult` does this for a match that was WON, as part of writing the result. This is the
   * same write for a match that ended any other way - a live match that ran out its ceiling
   * (`stalled`), which is archived as `played` and then never revisited. Without it the sweep's
   * work is thrown away for exactly the matches where something went wrong, which are the ones
   * somebody is most likely to open afterwards.
   *
   * Best effort and never throws, like every other history write: a board that could not be
   * stored is a detail view with one section missing, not a match anybody is held in.
   */
  function writeScoreboard(match) {
    try {
      if (!match) return;
      const board = scoreboardOf(match);
      const full = archived.get(match.id);
      if (!full) return;
      full.scoreboard = board;
      full.round_details = roundDetails(match);
      full.round_index_base = 0;
      full.rounds_played = full.round_details.length || null;
      for (const p of everyone(match)) {
        const mine = board.find((r) => r.player_id === p.player_id);
        if (!mine) continue;
        const rows = history.get(p.player_id) || [];
        const row = rows.find((r) => r && r.id === match.id);
        if (row) {
          row.kills = mine.kills; row.deaths = mine.deaths; row.team_kills = mine.team_kills;
        }
      }
      if (!store) return;
      const writes = [store(['SET', matchKey(match.id), JSON.stringify(full),
                             'EX', String(MATCH_TTL_SECONDS)])];
      for (const p of everyone(match)) {
        const mine = board.find((r) => r.player_id === p.player_id);
        if (!mine) continue;
        const key = historyKey(p.player_id);
        writes.push(Promise.resolve(store(['LRANGE', key, '0', String(HISTORY_KEEP - 1)]))
          .then((raw) => {
            if (!Array.isArray(raw)) return null;
            for (let i = 0; i < raw.length; i += 1) {
              let row;
              try { row = typeof raw[i] === 'string' ? identity.parse(raw[i]) : raw[i]; } catch { continue; }
              if (!row || row.id !== match.id) continue;
              row.kills = mine.kills; row.deaths = mine.deaths; row.team_kills = mine.team_kills;
              return store(['LSET', key, String(i), JSON.stringify(row)]);
            }
            return null;
          }));
      }
      Promise.all(writes).catch(() => { /* the in-memory copy still answers this process */ });
    } catch { /* a board that could not be written down cost nobody their exit */ }
  }

  /** The player's own last HISTORY_KEEP matches, newest first. Never throws. */
  async function readHistory(steamId) {
    if (store) {
      try {
        const raw = await store(['LRANGE', historyKey(steamId), '0', String(HISTORY_KEEP - 1)]);
        if (Array.isArray(raw)) {
          const rows = [];
          for (const item of raw) {
            try {
              const row = typeof item === 'string' ? identity.parse(item) : item;
              // A null or a stray scalar in the list would reach the hub as a row it cannot
              // read; drop it here rather than make every reader defensive.
              if (row && typeof row === 'object' && !Array.isArray(row)) rows.push(row);
            } catch { /* one bad row must not hide the other forty-nine */ }
          }
          // Result publication is repairable from the atomic receipt even if
          // the optional archive/history writes were interrupted by a restart.
          if (rows.length) {
            const receipts = await store(['MGET', ...rows.map(row => settlementKey(row.id))]);
            if (Array.isArray(receipts)) for (let i = 0; i < rows.length; i++) {
              if (!receipts[i]) continue;
              try { applyReceiptRow(rows[i], identity.parse(receipts[i]), steamId); } catch { /* keep original */ }
            }
          }
          return await restitution.annotate(rows);
        }
      } catch { /* fall through to memory */ }
    }
    return restitution.annotate((history.get(steamId) || []).slice(0, HISTORY_KEEP));
  }

  /** One full match, or null. Only the players who were in it may read it. */
  async function readMatch(matchId, steamId) {
    let full = null;
    let receipt = null;
    // Upstash first when it is there: it is the copy a later scoreboard writes the score into,
    // and the in-memory one would go on serving nulls for the life of the process.
    if (store) {
      try {
        const raw = await store(['GET', matchKey(matchId)]);
        if (raw) full = typeof raw === 'string' ? identity.parse(raw) : identity.hydrate(structuredClone(raw));
        const settled = await store(['GET', settlementKey(matchId)]);
        if (settled) receipt = typeof settled === 'string' ? identity.parse(settled) : identity.hydrate(structuredClone(settled));
      } catch { full = null; }
    }
    if (!full) full = archived.get(matchId) || null;
    if (receipt && receipt.publicMatch) full = structuredClone(receipt.publicMatch);
    if (!full || typeof full !== 'object') return null;
    identity.hydrate(full);
    const players = Array.isArray(full.players) ? full.players : [];
    if (!players.some((p) => p && p.player_id === steamId)) return null;
    if (receipt) receipt = await combatStorage.unpack(store, receipt, settlementKey(matchId));
    if (receipt) {
      full.won_team = receipt.winner;
      full.score = receipt.score;
      full.scoreboard = receipt.board || full.scoreboard;
      for (const p of players) applyReceiptRow(p, receipt, p.player_id, false);
    }
    // New display detail can be recovered from retained evidence. Preserve the
    // settled totals/rating decision and never mutate or expose the raw receipt.
    const retainedCombat = [...(receipt?.inputs?.combat_segments || []), receipt?.inputs?.combatState].filter(s=>s?.version===1);
    if (!Array.isArray(full.round_details)) {
      // Older collectors used inconsistent round indexing and mixed warmup into
      // stat round zero. Only an explicit marker permits native-data backfill.
      const indexed = full.round_index_base === 0 || receipt?.inputs?.round_index_base === 0;
      full = { ...full, round_details: roundDetails({
        ...(indexed ? receipt?.inputs || {} : {}), players, teams: full.teams, score: full.score,
        rounds_played: full.rounds_played,
        rounds: indexed ? receipt?.inputs?.rounds || full.rounds
          : (Array.isArray(full.rounds) ? full.rounds : []).filter(r => r && !Object.hasOwn(r, 'round')),
      }) };
    }
    if (retainedCombat.length && Array.isArray(full.scoreboard)) {
      full = { ...full, scoreboard: full.scoreboard.map(row => {
        if (!row?.combat || Array.isArray(row.combat.playerStats)) return row;
        const detail = combatLib.summaryAcross(retainedCombat, identity.gameFor(identity.freezeMatch(full), row.player_id), null);
        return { ...row, combat: { ...row.combat, playerStats: identity.combatSummary(full, detail).playerStats } };
      }) };
    }
    [full]=await restitution.annotate([full]);
    if(full.cheater_reverted){
      const cheaters=new Set(full.cheaters||[]);
      full.players=full.players.map(p=>({...p,cheater:cheaters.has(p.player_id),rr_delta:0,delta:0}));
      full.scoreboard=(full.scoreboard||[]).map(p=>({...p,cheater:cheaters.has(p.player_id)}));
    }
    return identity.wire(full);
  }

  function applyReceiptRow(row, receipt, steamId, historyRow = true) {
    const movement = receipt.rows.find(r => r.steamId === steamId);
    if (!movement) return;
    const side = (receipt.inputs.teams[1] || []).includes(steamId) ? 1 : 2;
    const rr = movement.rr || {};
    row.won = receipt.draw ? null : side === receipt.winner;
    row.rr_delta = Number.isFinite(rr.delta) ? rr.delta : null;
    row.delta = ratingLib.arrowsFor(row.rr_delta || 0);
    row.placement = Boolean(rr.placing || rr.placed);
    if (historyRow) row.score = `${receipt.score[side]}-${receipt.score[side === 1 ? 2 : 1]}`;
    const stats = (receipt.board || []).find(r => r.player_id === steamId);
    if (stats) for (const key of ['kills', 'deaths', 'team_kills']) row[key] = stats[key];
  }

  // ---------------------------------------------------------------- matchmaking
  /** The queue as matchmaker.cjs wants it: units carrying their members' current ratings. */
  function queueSnapshot() {
    return queue.map((unit) => ({
      key: unit.key,
      members:unit.members,
      ...(duel?{recent:recentDuels.get(unit.members[0])||[]}:{}),
      joined: unit.joined,
      ratings: unit.members.map((id) => ratingLib.ageUncertainty(ratingOf(id), Date.now())),
      network: unit.members.map((id) => networkRegistry.player(id)),
    }));
  }

  /**
   * Fisher-Yates, on a copy. Used on the two teams before the captains are named.
   *
   * The captain used to be whoever queued first on their side, because the teams were the
   * queue in order. The teams are chosen on rating now, so "first in the array" would mean
   * "the matchmaker happened to enumerate you first" - which is not random, it is just
   * arbitrary in a way nobody can see. Sam's brief says captains are chosen at random
   * (docs/competitive-ideas.md), so they are.
   */
  function shuffled(items) {
    const out = items.slice();
    for (let i = out.length - 1; i > 0; i -= 1) {
      const j = crypto.randomBytes(4).readUInt32BE(0) % (i + 1);
      [out[i], out[j]] = [out[j], out[i]];
    }
    return out;
  }

  /**
   * Ask the matchmaker for one match and, if it answers, make it real.
   *
   * What changed (2026-09-15): this used to be `queue.splice(0, MATCH_SIZE)` - the first ten
   * in join order, cut down the middle. It now hands the whole queue to matchmaker.findMatch,
   * which picks WHICH units play together (inside the rating gap each of them is currently
   * willing to accept) and WHICH five-and-five they split into (the most even of the 126, with
   * parties kept whole). Everything downstream - accept, lobby, connect - is untouched.
   *
   * Returns the match, or null when nothing forms. Null is the normal answer.
   */
  function tryFormMatch() {
    if(activityGate?.blocksFormation)return null;
    if (queuedPlayers() < MATCH_SIZE) return null;
    const teamSize = Math.max(1, Math.floor(MATCH_SIZE / 2));
    const picked = matchmaker.findMatch(queueSnapshot(), {
      now: Date.now(), matchSize: MATCH_SIZE, teamSize,
      ...(duel?{repeatCost:require('./duel-matching.cjs').cost}:{}),
    });
    // Nothing inside anybody's tolerance yet. They wait, their windows widen, the tick asks
    // again - and at TOL_OPEN_SECONDS the widest of them accepts anything at all.
    if (!picked) return null;

    // Map the matchmaker's units back onto real queue entries and pull them out.
    const byKey = new Map(queue.map((unit) => [unit.key, unit]));
    const teamOfId = new Map();
    const taken = [];
    // The matchmaker works on a snapshot, so its units carry a `key` and the ratings - not the
    // steamIds. `byKey` is how a decision comes back to the real queue entries it was made about.
    const roster = { 1: [], 2: [] };
    for (const side of [1, 2]) {
      for (const u of picked.teams[side] || []) {
        const unit = byKey.get(u.key);
        if (!unit) continue;
        taken.push(unit);
        roster[side].push(...unit.members);
        for (const id of unit.members) teamOfId.set(id, side);
      }
    }
    // A unit that vanished between the snapshot and here would make a short match. Nothing can
    // do that today (the snapshot and this run in one synchronous block), and if something ever
    // could, forming a 4v5 silently is the wrong way to find out.
    const size = taken.reduce((n, unit) => n + unit.members.length, 0);
    if (size !== MATCH_SIZE || teamOfId.size !== MATCH_SIZE
        || roster[1].length !== teamSize || roster[2].length !== MATCH_SIZE - teamSize
        || [...teamOfId.keys()].some(id => inMatch.has(id)) || bindingConflict([...teamOfId.keys()])) return null;
    const duelRules=duel?rulesExpected():null;
    if(duel&&!duelRules)return null;
    for (const unit of taken) removeFromQueue(unit.members[0]);

    const matchId = crypto.randomBytes(8).toString('hex');
    const players = [...teamOfId.keys()].map((steamId) => {
      const anyClient = clients.get([...(bySteam.get(steamId) || [])][0]);
      // personaOf/avatarOf, not the client alone. Everyone here just came out of the queue so a
      // live client is the normal answer - but an SSE reconnect landing on this instant would
      // write a BLANK onto the roster, and the roster is what the match record and its scoreboard
      // are built from, so that blank would outlive the match it was taken from.
      return { player_id: steamId, game_steam_id: gameOfPlayer(steamId), persona: (anyClient && anyClient.persona) || personaOf(steamId),
               // Carried on the match itself, not looked up when a payload is built: a player who
               // drops out of the lobby still has a face on the roster everyone else is looking at.
               avatar: (anyClient && anyClient.avatar) || avatarOf(steamId),
               accepted: false, connected: false };
    });
    const match = { id: matchId, mode: mode.id, players, state: 'found', created: Date.now(),
                    timer: null, deadline: 0, map: '', host: picked.network ? picked.network.host : '',
                    network: picked.network || null,
                    network_preferences: Object.fromEntries(taken.flatMap(u => u.members).map(id =>
                      [id, networkRegistry.preferences.get(id) || null])),
                    // Anyone who walks out mid-match is removed from `players` so the accept and
                    // connect counts stay right, but they were still IN this match: the record
                    // (and the abandon ladder, C6-C9) needs them, so they are kept here.
                    left: [],
                    // The matchmaker's decision, kept for the lobby to build teams from and for
                    // the result to settle against. NONE of this is sent to a client before the
                    // match is played - see the NEVER PRICE A MATCH note at the top of the file.
                    mm: {
                      // Shuffled so the captain (teams[n][0]) is a random member of the side
                      // rather than whichever unit the search enumerated first.
                      teams: { 1: shuffled(roster[1]), 2: shuffled(roster[2]) },
                      delta: Math.round(picked.delta),
                      spread: Math.round(picked.spread),
                      tolerance: Number.isFinite(picked.tolerance) ? Math.round(picked.tolerance) : null,
                      waited: Math.round(picked.waited),
                      quality: picked.quality,
                      ratings: picked.ratings,
                      parties: taken.filter((u) => u.members.length > 1).map((u) => u.members.slice()),
                    } };
    // Freeze duel rules before the lobby so a release cannot change a formed match.
    if(duel){match.expected_score_limit=duelRules.score_limit;match.expected_max_rounds=duelRules.max_rounds;}
    matches.set(matchId, match);
    note(match, 'formed', { quality: picked.quality, wait_seconds: picked.waited, match_size: size });
    for (const p of players) inMatch.set(p.player_id, matchId);

    const found = {
      type: 'match_found', match_id: matchId, accept_seconds: ACCEPT_SECONDS,
      network: match.network,
      players: players.map((p) => ({ player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id), persona: p.persona, avatar: p.avatar || '' })),
    };
    for (const p of players) sendTo(p.player_id, found);
    broadcast(stats());

    arm(match, ACCEPT_SECONDS, 'accept', () => expireAccept(matchId));
    return match;
  }

  /**
   * Keep forming matches until the queue stops yielding them. One tick may make several: ten
   * people accepting at once is exactly when there is a backlog to clear.
   *
   * Capped so a bug in findMatch - one that returns a match without draining the queue - costs
   * a slow tick rather than a wedged replica.
   */
  function drainQueue() {
    let formed = 0;
    while (formed < 16 && tryFormMatch()) formed += 1;
    return formed;
  }

  // ---------------------------------------------------------------- surviving a redeploy
  // matchId -> the JSON last written for it, so the sweep below writes only what CHANGED. A
  // live match is touched every few seconds by score reports; re-sending an identical body
  // every tick would be one Upstash round trip per match per tick for nothing.
  const persisted = new Map();
  function trackWrite(map, id, promise) {
    let pending = map.get(id);
    if (!pending) { pending = new Set(); map.set(id, pending); }
    const safe = Promise.resolve(promise).catch(() => {});
    pending.add(safe);
    safe.finally(() => { pending.delete(safe); if (!pending.size) map.delete(id); });
    return safe;
  }

  /**
   * A match as JSON: everything on it except the two timers, which are this process's and mean
   * nothing to the next one (`deadline` and `ban_deadline` are absolute and do carry).
   *
   * Written as a SWEEP over the object's own keys rather than a field list, on purpose. This
   * file grows - `ingame`, `rounds`, `kills` and `teams_verdict` were all added to a match after
   * it was first built - and a hand-kept list would go quietly stale, which is the failure where
   * a redeploy keeps the match but loses the score. Maps and Sets are tagged so they survive the
   * round trip as Maps and Sets; a plain JSON.stringify turns both into `{}`.
   */
  function serialiseMatch(match) {
    const out = {};
    for (const [key, value] of Object.entries(match)) {
      if (['timer', 'ban_timer', 'stage_timer', 'collectTimer', 'settling'].includes(key)) continue;
      if (value instanceof Map) out[key] = { __map: [...value.entries()] };
      else if (value instanceof Set) out[key] = { __set: [...value] };
      else out[key] = value;
    }
    return out;
  }

  /** The inverse. Timers come back null and rearm() puts the real ones on. */
  function reviveMatch(raw) {
    if (rankedModes.modeOf(raw?.mode).id !== mode.id) throw Error('Saved match belongs to another ranked mode');
    const out = { timer: null, ban_timer: null, stage_timer: null };
    for (const [key, value] of Object.entries(raw || {})) {
      if (value && typeof value === 'object' && Array.isArray(value.__map)) out[key] = new Map(value.__map);
      else if (value && typeof value === 'object' && Array.isArray(value.__set)) out[key] = new Set(value.__set);
      else out[key] = value;
    }
    // Before first-to-five, duel rules were only saved when connecting began.
    // Existing found/ready lobbies still have the first-to-seven pack installed.
    if(duel && out.expected_score_limit===undefined && out.expected_max_rounds===undefined){
      out.expected_score_limit=7;out.expected_max_rounds=13;
    }
    return identity.freezeMatch(out);
  }

  /**
   * Write through every match whose state has moved, and forget the ones that have ended.
   *
   * Run from the match tick (every MATCH_TICK_MS, 2 s by default), which means at most one tick's
   * worth of a match can be lost to a container being killed outright - a score report or two,
   * never the match. Everything here is fire-and-forget: a store that is slow or missing must
   * never hold up the tick that forms matches.
   */
  function persistLive(match, snapshotOptions) {
    if (!store) return Promise.resolve();
    const json = JSON.stringify(serialiseMatch(match));
    const pending = (matchWriting.get(match.id) || Promise.resolve()).catch(() => {}).then(async () => {
      const receipt = await settlementLib.snapshot(store,
        [settlementKey(match.id), liveMatchKey(match.id), liveIndexKey(), authorityKey(match.id)], match.id, json, LIVE_STATE_TTL_SECONDS, snapshotOptions);
      if (receipt && receipt.pendingMatch) {
        for (const key of ['timer', 'collectTimer', 'stage_timer', 'ban_timer']) if (match[key]) clearTimeout(match[key]);
        if (receipt.pendingMatch.void_pending) delete match.collecting;
        if (match.void_pending && !receipt.pendingMatch.void_pending) {
          delete match.void_pending;
          delete match.void_reason;
          if (match.final_snapshot === JSON.stringify({ voided: true })) delete match.final_snapshot;
        }
        Object.assign(match, reviveMatch(receipt.pendingMatch));
        persisted.set(match.id, JSON.stringify(receipt.pendingMatch));
        if (!match.settling) rearm(match);
        return;
      }
      if (receipt) {
        if (receipt.data_collected === true) { await acceptCommitted(match, receipt); return; }
        if (!match.settling && matches.get(match.id) === match) {
          hydrateSettlement(receipt.rows);
          completeMatch(match, receipt.winner, receipt.score, receipt.limit, receipt.rows);
        }
        return;
      }
      persisted.set(match.id, json);
    });
    matchWriting.set(match.id, pending);
    pending.finally(() => { if (matchWriting.get(match.id) === pending) matchWriting.delete(match.id); }).catch(() => {});
    return pending;
  }

  function flushMatches() {
    if (!store) return Promise.resolve();
    const writes = [];
    for (const match of matches.values()) {
      if(match.terminal){writes.push(matchOperation(match.id,()=>finishDuelDecision(match)));continue;}
      if (match.void_pending) {
        writes.push(matchOperation(match.id, () => finishVoidVote(match)));
        continue;
      }
      if (match.settling || match.final_snapshot) continue;
      writes.push(persistLive(match));
      if (!match.start_ready_verified && match.collecting && match.collecting.deadline <= Date.now()) {
        const c = match.collecting;
        finishMatch(match, c.winner, c.score, c.limit);
      }
    }
    // ...and anything we were keeping that is no longer a match. forgetMatch() already runs at
    // each of the three places a match is removed; this is the net under them, for a match that
    // left some other way.
    for (const id of [...persisted.keys()]) if (!matches.has(id)) forgetMatch(id);
    return Promise.allSettled(writes);
  }

  /** This match is over: drop the live copy so a boot can never bring it back. */
  function forgetMatch(matchId) {
    persisted.delete(matchId);
    if (!store) return;
    const pending = (matchWriting.get(matchId) || Promise.resolve()).catch(() => {}).then(async () => {
      await store(['EVAL',require('./migration.cjs').FORGET,'4',liveMatchKey(matchId),liveIndexKey(),authorityKey(matchId),settlementKey(matchId),matchId],{strict:true});
    });
    matchWriting.set(matchId, pending);
    pending.finally(() => { if (matchWriting.get(matchId) === pending) matchWriting.delete(matchId); }).catch(() => {});
  }

  // The states a match can be restored INTO. Anything else is either finished or a state with
  // nobody waiting in it, and bringing one back would put ten people into a match that is over.
  const LIVE_STATES = new Set(['found', 'ready', 'lobby', 'connecting', 'live']);

  /**
   * Read back the matches the previous container was running, and put them on their clocks.
   *
   * Runs once, at start. `ready` is what the routes wait on, so a hub that reconnects in the
   * first moments of a new container is not told it is in no match while the answer is still in
   * flight - which would be the same bug with a smaller window.
   *
   * A match is skipped when it is not one anybody can still be waiting in: no id, no players, or
   * a state this module does not run a clock for. Anything restored is exactly the object that
   * was stored, so the replay in handleStream hands each player the phase they were actually in.
   */
  async function restoreMatches() {
    if (!store) return 0;
    const ids = await store(['SMEMBERS', liveIndexKey()], { strict: true });
    if (!Array.isArray(ids)) throw new Error('Invalid live match index');
    const recovered = [];
    let back = 0;
    for (const id of ids) {
      if (matches.has(String(id))) continue;
      const raw = await store(['GET', liveMatchKey(String(id))], { strict: true });
      if (!raw) {                           // expired, or written by a run that never finished
        await store(['SREM', liveIndexKey(), String(id)], { strict: true });
        continue;
      }
      const receipt = await store(['GET', settlementKey(String(id))], { strict: true });
      if (receipt) {
        await settlementLib.snapshot(store, [settlementKey(String(id)), liveMatchKey(String(id)), liveIndexKey()],
          String(id), raw, LIVE_STATE_TTL_SECONDS);
        continue;
      }
      let match, snapshot;
      try { snapshot = identity.parse(raw); match = reviveMatch(snapshot); } catch { match = null; }
      if (!match || !match.id || !Array.isArray(match.players) || !match.players.length) {
        forgetMatch(String(id));
        continue;
      }
      if (!LIVE_STATES.has(match.state)) { forgetMatch(match.id); continue; }
      const savedAuthority=await store(['GET',authorityKey(match.id)],{strict:true});
      if(savedAuthority && identity.parse(savedAuthority).closed) {forgetMatch(match.id);continue;}
      // Missing evidence is retryable storage failure, never a reason to discard a match.
      snapshot = await combatStorage.unpack(store, snapshot, liveMatchKey(String(id)));
      match = reviveMatch(snapshot);
      // A previous process may have completed while an older live SET was in flight.
      const completed = await store(['GET', `${prefix || 'hub:'}result:${match.id}`], { strict: true });
      if (completed) { forgetMatch(match.id); continue; }
      // Current connecting/live snapshots always carry a credential. Its absence identifies a
      // pre-feature active match. Preserve the marker across later restores even after a reconnect
      // lazily creates a token that the already-running old pak cannot know.
      if (match.legacyReportAuth === true ||
          (['connecting', 'live'].includes(match.state) && !snapshot.reportToken)) {
        match.legacyReportAuth = true;
      }
      // The clock gets its floor back before anything is armed (RECOVERY_GRACE_SECONDS), and the
      // match remembers that it was recovered - the archive is the only place that can explain
      // an odd-looking window later.
      const floor = Date.now() + RECOVERY_GRACE_SECONDS * 1000;
      if (match.deadline && match.deadline < floor) match.deadline = floor;
      if (match.lobby && match.lobby.ban_deadline && match.lobby.ban_deadline < floor) {
        match.lobby.ban_deadline = floor;
      }
      match.recovered = Date.now();
      // A game can finish before any hub reconnects. Never use new-player
      // defaults for the restored roster; failed reads remain retryable.
      await loadRatings(match.players.map(p => p.player_id));
      recovered.push(match);
    }
    // Admit nobody until the complete inventory has been read successfully.
    for (const match of recovered) {
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.player_id, match.id);
      // Remember what we read, so the first sweep does not rewrite an unchanged match.
      try { persisted.set(match.id, JSON.stringify(serialiseMatch(match))); } catch { /* it will rewrite */ }
      rearm(match);
      back += 1;
    }
    if (back) console.log(`[live] recovered ${back} match(es) from the last deploy`);
    return back;
  }

  let recoveryComplete = !store;
  let recoveryPending = null;
  function ensureRecovery() {
    if (recoveryComplete) return Promise.resolve(0);
    if (recoveryPending) return recoveryPending;
    recoveryPending = restoreMatches().then(n => { recoveryComplete = true; return n; })
      .finally(() => { recoveryPending = null; });
    return recoveryPending;
  }
  const ready = ensureRecovery().catch(() => 0);

  // The clock the widening tolerance needs (see MATCH_TICK_MS). unref'd so it can never be the
  // thing keeping a test process alive, and cleared in shutdown().
  const matchTick = setInterval(() => {
    networkRegistry.prune();
    if (!recoveryComplete) { ensureRecovery().catch(() => {}); return; }
    try { drainQueue(); } catch { /* one bad tick must not stop the next */ }
    // ...and the same tick is what keeps the live matches on disk, so a redeploy costs at most
    // one tick of a match rather than the whole thing (see flushMatches).
    try { flushMatches(); } catch { /* likewise */ }
    checkHostTimeouts().catch(()=>{});
  }, MATCH_TICK_MS);
  if (typeof matchTick.unref === 'function') matchTick.unref();

  /** Append one row to this match's diagnostic timeline, which is archived with it.
   *
   * Deliberately tiny and deliberately total: every clock change and every close goes through
   * here, so the archived row can answer "what actually happened to this match" without anyone
   * having caught the server's stdout at the time. See the `diag` field in fullRecord. */
  function analyticsContext(match) {
    const ratings=match.mm?.ratings;
    return { rules: { ...require('./analytics.cjs').ruleSnapshot(), game: rulesExpected() },
      deployment: process.env.RAILWAY_GIT_COMMIT_SHA || 'local', hub: requireVersions()?.hub || 'unknown', mode: mode.id,
      balance:duel?{initial_host:match.initial_host||null,starting_sides:match.starting_sides||null,host_changed:Boolean(match.host_migrations?.length)}:null,
      region: match.network?.region || 'unknown',
      predicted_win: ratings?.[1] && ratings?.[2] ? ratingLib.winProbability(ratings[1],ratings[2]) : null };
  }
  function note(match, ev, extra) {
    if (!match) return;
    analytics?.emit('match.' + ev.replace(/[^a-z0-9_]/gi, '_').toLowerCase(), extra || {}, { match_id: match.id });
    if (!Array.isArray(match.diag)) match.diag = [];
    if (match.diag.length > 60) return;          // bounded; a match cannot generate a log flood
    match.diag.push({ t: Date.now(), ev, ...(extra || {}) });
  }

  /** One timer per match, always replaced, never left behind.
   *
   * `kind` names WHICH clock this is ('accept', 'lobby', 'connect', 'live', 'teams'). It is
   * stored on the match because a container that has just started has the deadline but not the
   * closure that went with it, and guessing the callback from `state` would be wrong the moment
   * two clocks shared a state - which the teams gate already does, inside `connecting`. See
   * EXPIRIES / rearm below. */
  function arm(match, seconds, kind, fn) {
    if (match.timer) clearTimeout(match.timer);
    match.deadline = Date.now() + seconds * 1000;
    match.expiry = kind;
    match.timer = setTimeout(fn, seconds * 1000);
    if (typeof match.timer.unref === 'function') match.timer.unref();
    // WHICH clock, for HOW long, and how far into the match it was set. arm() owns the single
    // timer a match has, so a short clock overwriting a long one is visible here and nowhere else.
    note(match, 'arm', { kind, seconds, age: Math.round((Date.now() - (match.created || 0)) / 1000),
                         state: match.state, n: (match.players || []).length });
    console.log('[clock] %s armed %s for %ss (state=%s)', match.id, kind, seconds, match.state);
  }

  // Every clock a match can be on, by the name arm() stored. A match restored from Upstash is
  // put back on its own one; anything not in here simply has no clock, which is what a match
  // in a state nobody can be stuck in should have.
  const EXPIRIES = {
    accept: (id) => expireAccept(id),
    lobby: (id) => expireLobby(id),
    connect: (id) => expireConnect(id),
    live: (id) => expireLive(id),
    teams: (id) => expireTeamsGate(id),
  };

  /** Put a restored match back on the clock it was on, using the deadline it was stored with.
   *
   * NOT arm(): arm measures a fresh window from now, and a match that has been running for four
   * minutes of its five-minute connect window must get the remaining one, not another five. A
   * deadline already in the past fires at once, which is the correct answer - that expiry was
   * owed while the container was starting. */
  function rearm(match) {
    if(match.recovery?.phase==='restoring')return;
    if (match.final_snapshot || match.terminal || match.void_pending) return;
    if (match.collecting && !match.start_ready_verified) {
      const c = match.collecting;
      c.deadline = Number(c.deadline) || Number(c.since) + collectFor * 1000;
      match.collectTimer = setTimeout(() => {
        match.collectTimer = null;
        finishMatch(match, c.winner, c.score, c.limit);
      }, Math.max(0, c.deadline - Date.now()));
      match.collectTimer.unref?.();
      return;
    }
    // A RESTORED match, and therefore a container that restarted. Nothing else in the timeline can
    // say this: a Railway restart without a new deployment is invisible from the outside, and it
    // is one of the few mechanisms that could end a 180-second window in fifty. If this row is
    // absent from a short cancellation, the restart theory is dead.
    note(match, 'rearm', { kind: match.expiry || '', state: match.state,
                           leftOnClock: match.deadline
                             ? Math.round((match.deadline - Date.now()) / 1000) : null });
    const fire = EXPIRIES[match.expiry];
    if (fire && match.deadline) {
      if (match.timer) clearTimeout(match.timer);
      match.timer = setTimeout(() => fire(match.id), Math.max(0, match.deadline - Date.now()));
      if (typeof match.timer.unref === 'function') match.timer.unref();
    }
    // The lobby stage has its own clock (see armStageTurn), so a lobby in progress has to get it
    // back too, or whoever it is waiting on gets an unlimited turn.
    //
    // `ban_deadline` is the old name for the same field, read here so a match written by the
    // previous release - one that only ever clocked the veto - comes back on a clock instead of
    // silently losing it for the rest of that turn.
    const L = match.lobby;
    const deadline = L && (L.stage_deadline || L.ban_deadline);
    if (L && deadline && stageSeconds(L.stage)) {
      L.stage_deadline = deadline;
      if (match.stage_timer) clearTimeout(match.stage_timer);
      match.stage_timer = setTimeout(() => expireStageTurn(match.id),
                                     Math.max(0, deadline - Date.now()));
      if (typeof match.stage_timer.unref === 'function') match.stage_timer.unref();
    }
  }

  function expireAccept(matchId) {
    const match = matches.get(matchId);
    if (!match || match.state !== 'found') return;
    const declined = match.players.filter((p) => !p.accepted).map((p) => p.player_id);
    closeMatch(match, 'declined', declined);
  }

  /**
   * The lobby stalled. The coin flip and the veto still run in the hub, so a match sitting
   * in `ready` past its deadline means OUR code lost the thread, not that anyone misbehaved.
   * Release everyone, blame nobody, put them all back in the queue.
   */
  /** Arm the clock on the CURRENT ban turn. Its own timer, not `match.timer`.
   *
   * arm() owns the one timer a match has, and that one is the LOBBY deadline - the backstop that
   * ends a lobby nobody is playing at all. Re-using it here would mean each ban silently pushed
   * the lobby deadline out, so a veto could outlive the thing meant to bound it. */
  function stageSeconds(stage) {
    if (stage === 'veto') return BAN_SECONDS;
    if (stage === 'flipping') return FLIP_SECONDS;
    if (stage === 'coin' || stage === 'choice' || stage === 'side') return PICK_SECONDS;
    return 0;
  }

  /** Arm the clock on the CURRENT lobby stage. Its own timer, not `match.timer`.
   *
   * ONE timer for all of them rather than one per stage: they are strictly sequential, only ever
   * one of them is running, and a single timer cannot leak the way four could. */
  function armStageTurn(match) {
    clearStageTurn(match);
    const L = match.lobby;
    if (!L) return;
    const seconds = stageSeconds(L.stage);
    if (!seconds) return;
    L.stage_deadline = Date.now() + seconds * 1000;
    L.ban_deadline = L.stage_deadline;   // the old name, for a hub or a stored match on the last release
    match.stage_timer = setTimeout(() => expireStageTurn(match.id), seconds * 1000);
    if (typeof match.stage_timer.unref === 'function') match.stage_timer.unref();
  }

  function clearStageTurn(match) {
    if (match && match.stage_timer) {
      clearTimeout(match.stage_timer);
      match.stage_timer = null;
    }
    if (match && match.lobby) {
      match.lobby.stage_deadline = 0;
      match.lobby.ban_deadline = 0;
    }
  }

  /** A lobby stage ran out. Make the choice the absent captain declined to make and carry on.
   *
   * Every branch takes the SAME path a real click takes (applyCoin / applyAdvantage / applySide /
   * applyBan), flagged `auto` so the hub can say so. Keeping the two paths on one piece of code is
   * what stops them drifting: an auto-choice that forgot to arm the next stage's clock would strand
   * the lobby in a quieter, harder-to-find version of the problem this whole clock exists to fix. */
  function expireStageTurn(matchId) {
    const match = matches.get(matchId);
    if (!match || match.state !== 'ready' || !match.lobby) return;
    const L = match.lobby;
    if (L.stage === 'coin') {
      // A random call on a random face is still a fair coin, so nobody is worse off for it.
      applyCoin(match, (crypto.randomBytes(1)[0] & 1) ? 'heads' : 'tails', true);
    } else if (L.stage === 'flipping') {
      landCoin(match);                 // nobody stalled: the coin simply finished its arc
    } else if (L.stage === 'choice') {
      applyAdvantage(match, (crypto.randomBytes(1)[0] & 1) ? 'side' : 'ban', true);
    } else if (L.stage === 'side') {
      applySide(match, (crypto.randomBytes(1)[0] & 1) ? 'attack' : 'defend', true);
    } else if (L.stage === 'veto') {
      const remaining = lobbyRemaining(match);
      if (remaining.length <= 1) return;
      applyBan(match, shuffled(remaining)[0], true);
    }
  }

  /** The one place a ban is recorded, whoever decided it. Keeping the auto path and the human path
   * on the same code is what stops them drifting - an auto-ban that forgot to advance the turn, or
   * to end the veto on the last map, would strand the lobby in a different way. */
  function applyBan(match, name, auto) {
    const L = match.lobby;
    L.bans.push({ team: L.ban_turn, map: name, auto: Boolean(auto) });
    const left = lobbyRemaining(match);
    if (left.length === 1) {
      L.map = left[0];
      L.stage = 'ready';              // the lobby is decided; a client opens the connect window
      clearStageTurn(match);
    } else {
      L.ban_turn = L.ban_turn === 1 ? 2 : 1;
      armStageTurn(match);
    }
    broadcastLobby(match);
  }

  function expireLobby(matchId) {
    const match = matches.get(matchId);
    if (!match || match.state !== 'ready') return;
    clearStageTurn(match);
    closeMatch(match, 'stalled', []);
  }

  /**
   * The connect window ran out. Exactly Sam's rule: the match dies, the people who made it
   * into the game lose nothing at all, and the ones who never turned up pay.
   *
   * WHEN THE HOST NEVER TURNED UP, THE HOST IS THE ONLY ONE WHO DID ANYTHING WRONG. A joiner
   * cannot begin to join until the host's game is up and findable - that is the whole reason
   * CONNECT_SECONDS re-bases itself at reportConnected - so a joiner sitting on a grey Launch
   * button for five minutes has not missed a window, they were never given one. Blaming them
   * is the same mistake as blaming somebody for a match that was never made.
   */
  function expireConnect(matchId) {
    const match = matches.get(matchId);
    if (!match || match.state !== 'connecting') return;
    const host = String(match.host || '');
    const hostUp = match.players.some((p) => String(p.player_id) === host && p.connected);
    const missing = match.players
      .filter((p) => !p.connected)
      .filter((p) => hostUp || String(p.player_id) === host)
      .map((p) => p.player_id);
    const punished = {};
    for (const steamId of missing) punished[steamId] = applyNoShow(steamId);
    closeMatch(match, missing.length ? 'no_show' : 'stalled', missing, punished);
  }

  /**
   * A live match ran out its ceiling. Nobody did anything wrong - there is no way for anyone to
   * END a match yet - so this closes it, frees every player from `inMatch`, and blames nobody.
   * `stalled` is the honest reason: it is our code that never reported back, and the hub
   * already tells the player exactly that and puts them back in the queue.
   *
   * It was archived as `played` the moment it went live, and archiveMatch is idempotent, so
   * closing it here does NOT rewrite that row as a cancellation.
   */
  function expireLive(matchId) {
    const match = matches.get(matchId);
    if (!match || match.state !== 'live' || match.final_snapshot || match.collecting || match.terminal || match.void_pending) return;
    if(duel)return matchOperation(match.id,()=>voidServiceFailure(match));
    closeMatch(match, 'stalled', []);
  }

  function closeMatch(match, reason, blame = [], punished = {}) {
    if (matches.get(match.id)!==match || match.collecting || match.settling || match.final_snapshot || match.terminal || match.void_pending) return;
    if(store && match.migration_capabilities && !approvedClosures.has(match)) {
      const host=match.host,epoch=match.host_epoch||0;
      return matchOperation(match.id,async()=>{
        if(matches.get(match.id)!==match || match.collecting || match.settling || match.final_snapshot || match.terminal || match.void_pending ||
           match.host!==host || (match.host_epoch||0)!==epoch)return;
        const closed=await store(['EVAL',require('./migration.cjs').CLOSE,'2',authorityKey(match.id),settlementKey(match.id),
          host,String(epoch),String(Date.now()),'0',String(LIVE_STATE_TTL_SECONDS)],{strict:true});
        if(closed!==1)return;
        approvedClosures.add(match);
        closeMatch(match,reason,blame,punished);
      }).catch(()=>{});
    }
    // The lobby stage clock is a SECOND timer, and a match can be closed from a dozen places that
    // never went near the lobby (a decline, a no-show, a redeploy). Left running it would fire
    // expireStageTurn on a dead match - harmless today, only because that function re-checks the
    // state - and hold the match object alive until it did.
    clearStageTurn(match);
    revokeHostPermit(match.host);
    for (const p of match.players) revokeJoinPermit(p.player_id);
    // THE CALLER, and it is the whole point of this row. `no_show` is reachable from exactly two
    // places - expireConnect, which blames every unconnected player, and handleMatchLeave, which
    // blames one - and the archived record alone cannot tell them apart when a 2-player match has
    // both players unconnected. The stack frame can.
    let from = '';
    try { from = String((new Error()).stack || '').split('\n')[2].trim().slice(0, 120); }
    catch { from = 'unknown'; }
    note(match, 'close', { reason, from, blame: blame.slice(0, 10),
                           armed: match.expiry || '',
                           left: match.deadline ? Math.round((match.deadline - Date.now()) / 1000) : null,
                           age: Math.round((Date.now() - (match.created || 0)) / 1000),
                           state: match.state });
    console.log('[close] %s reason=%s from=%s armed=%s leftOnClock=%ss',
                match.id, reason, from, match.expiry || '-',
                match.deadline ? Math.round((match.deadline - Date.now()) / 1000) : '-');
    if (match.timer) clearTimeout(match.timer);
    match.timer = null;
    match.state = 'cancelled';
    for (const p of match.players) inMatch.delete(p.player_id);
    matches.delete(match.id);
    hostChecks.delete(match.id);
    forgetMatch(match.id);          // and the live copy, so no boot can bring it back
    // Only now, with the players already released and the match already out of the registry:
    // archiving needs nothing from either, so writing history can never be what keeps ten
    // people pinned in a match that has ended. A cancelled match is still a match they were
    // in, and "you no-showed on Rome and it cost you five minutes" is the row they most need.
    archiveMatch(match, { outcome: 'cancelled', reason, blame, punished });
    // Terminal evidence is independent of ranking and survives worker retries once saved.
    try { analytics?.terminal?.({ matchId: match.id, at: Date.now(),
      analytics_context: { ...analyticsContext(match), terminal: true },
      inputs: { teams: match.teams || match.mm?.teams, mm: match.mm },
      publicMatch: fullRecord(match, { ended: Date.now(), outcome: 'cancelled', reason }),
      board: scoreboardOf(match) }); } catch { /* diagnostic failure cannot stop releasing players */ }
    // A cancelled match has no time played in it and no result, but it does have a column of its
    // own: who walked out of it, and who never turned up.
    creditMatch(match, { played: false, reason, blame });
    // A LIVE match that ran out its ceiling was archived as `played` at go-live, so the call
    // above is a no-op for it (archiveMatch is idempotent) - and without this its scoreboard
    // would be the empty one written before anybody had spawned. `stalled` is the honest reason
    // and a stalled match is still a match that was played; what the sweep saw of it is kept.
    if (match.archived) writeScoreboard(match);
    // A player who did their part goes back to the FRONT of the queue; that is the FACEIT
    // behaviour and the only fair one, since they did nothing wrong. "Did their part" means
    // accepted for a decline, and actually connected for a no-show.
    //
    // "The front" is not a position any more - the queue is a pool and the matchmaker sorts on
    // how long each unit has waited. So it is granted as a CLOCK instead: a requeued unit is
    // handed a `joined` that is already TOL_OPEN_SECONDS old, which makes it the anchor of the
    // next search and gives it the widest tolerance there is. Being sent back to the front now
    // means "you are first AND you will take whatever there is", which is exactly what somebody
    // who just lost twenty seconds to another player's dodge is owed.
    const backOfNothing = Date.now() - matchmaker.TOL_OPEN_SECONDS * 1000;
    const regroup = new Map();          // party code (or the player's own id) -> [steamId, ...]
    const requeued = new Set();
    for (const p of match.players) {
      const blamed = blame.includes(p.player_id);
      const innocent = !blamed && (reason === 'no_show' || reason === 'stalled' ? true : p.accepted);
      if (innocent && bySteam.has(p.player_id) && !isQueued(p.player_id)) {
        // Back together, if they came in together. Requeuing a five-stack as five solos would
        // let the next search cut them across the two teams, which is the one thing C10 forbids.
        const code = partyOf.get(p.player_id) || '';
        const key = code || `solo:${p.player_id}`;
        if (!regroup.has(key)) regroup.set(key, { code, members: [] });
        regroup.get(key).members.push(p.player_id);
      }
    }
    for (const { code, members } of regroup.values()) {
      const party = code ? parties.get(code) : null;
      if (code && (!party || party.members.length !== members.length || party.members.some(id=>!members.includes(id)))) continue;
      if (enqueue(members, code, backOfNothing)) for (const id of members) requeued.add(id);
    }
    for (const p of match.players) {
      sendTo(p.player_id, {type:'match_cancelled',match_id:match.id,reason,
        requeued:requeued.has(p.player_id),blamed:blame.includes(p.player_id),penalty:punished[p.player_id]||null});
    }
    for (const id of requeued) {
        sendTo(id, { type: 'queued', position: queuePosition(id), size: queuedPlayers() });
    }
    broadcast(stats());
    drainQueue();
  }

  function acceptMatch(steamId) {
    const matchId = inMatch.get(steamId);
    const match = matchId && matches.get(matchId);
    if (!match || match.state !== 'found') return { ok: false, error: 'No match to accept.' };
    const player = match.players.find((p) => p.player_id === steamId);
    if (!player) return { ok: false, error: 'You are not in that match.' };
    player.accepted = true;

    const accepted = match.players.filter((p) => p.accepted).length;
    const progress = { type: 'match_accept', match_id: match.id, accepted, total: match.players.length };
    for (const p of match.players) sendTo(p.player_id, progress);

    if (accepted === match.players.length) {
      match.state = 'ready';
      // The lobby is decided HERE now (coin flip, side/ban choice, map veto): build its state,
      // designate the one captain who flips, and hand every client the same starting point. The
      // server keeps its hand on the clock: if the lobby never finishes, expireLobby() lets go.
      match.lobby = buildLobby(match);
      arm(match, LOBBY_SECONDS, 'lobby', () => expireLobby(match.id));
      // The coin call is a turn like any other, so it is on a clock from the moment the lobby
      // exists. This is the stall that used to cost a match: nobody could do ANYTHING until the
      // one designated captain clicked, and nothing made them.
      armStageTurn(match);
      for (const p of match.players) {
        sendTo(p.player_id, { type: 'match_ready', match_id: match.id,
                             lobby_seconds: LOBBY_SECONDS,
                             players: match.players.map((q) => ({ player_id: q.player_id, game_steam_id: identity.gameFor(match, q.player_id), persona: q.persona, avatar: q.avatar || '' })),
                             ...lobbyPayload(match) });
      }
    }
    return { ok: true, accepted, total: match.players.length };
  }

  // ---------------------------------------------------------------- server-authoritative lobby
  // Sam's flow (docs/competitive-ideas.md): a visible coin flip, then the toss winner takes the
  // starting SIDE or the last BAN, then an alternating map veto down to one map. It used to run
  // on every client with its own random.choice, so ten players saw ten different winners. It
  // runs here now: ONE captain flips, the server decides once, and everyone is told the same.

  /** Split the roster into two teams and name the captains. The ONE captain who flips the coin
   * for the whole match is a deterministic, server-owned choice - the captain of team 1 - so
   * every client agrees who it is without any negotiation. */
  function buildLobby(match) {
    const ids = match.players.map((p) => p.player_id);
    // THE TEAMS WERE ALREADY DECIDED, at formation, by matchmaker.bestSplit - the most even of
    // the 126 ways to cut ten players in two, with every party whole on one side (C10). They
    // are only read back here. Filtered against the live roster because a player can walk out
    // between the accept window and the lobby, and the shape below has to hold whatever is left.
    const mm = match.mm || {};
    let team1 = (mm.teams && mm.teams[1] || []).filter((id) => ids.includes(id));
    let team2 = (mm.teams && mm.teams[2] || []).filter((id) => ids.includes(id));
    // The fallback is the old behaviour, and it is not dead code: a match resumed from a state
    // written before this shipped has no `mm`, and so does anything that ever builds a match by
    // another route. Half in order beats no lobby at all.
    if (team1.length + team2.length !== ids.length) {
      const half = Math.max(1, Math.floor(ids.length / 2));  // 10 -> 5, 2 -> 1, 1 -> 1 (t2 empty)
      const assigned = new Set([...team1, ...team2]);
      const spare = ids.filter((id) => !assigned.has(id));
      while (team1.length < half && spare.length) team1.push(spare.shift());
      team2 = team2.concat(spare);
    }
    const captains = { 1: team1[0] || '', 2: team2[0] || team1[0] || '' };
    return {
      teams: { 1: team1, 2: team2 },
      captains,
      coin_captain: captains[1],       // the single designated captain: team 1's
      stage: 'coin',                   // coin -> flipping -> choice -> side -> veto -> ready
      // Which of the four decisions the CLOCK made rather than a captain. Same idea as a ban's
      // `auto`: a teammate who comes back to find their side already picked should be told the
      // clock did it, not left thinking their captain chose badly.
      coin_auto: false, advantage_auto: false, side_auto: false,
      stage_deadline: 0, ban_deadline: 0,
      coin_side: null, coin_result: null, toss_winner: null,
      advantage: null,                 // what the toss winner chose: 'side' | 'ban'
      side_picker: null,               // the team that picks attack/defend
      ban_advantage: null,             // the team that bans LAST (controls the final map)
      sides: { 1: '', 2: '' },
      first_ban: null, ban_turn: null,
      bans: [],                        // [{ team, map }]
      pool: COMP_MAP_POOL.slice(),
      map: null,
    };
  }

  /** The lobby as every client reads it. Flat keys so it can be spread into match_ready and the
   * `lobby` broadcast alike; the hub's _apply_lobby writes the lot. */
  function lobbyPayload(match) {
    const L = match.lobby || {};
    return {
      host: match.network ? match.network.host : '',
      network: match.network || null,
      teams: L.teams ? { 1: L.teams[1] || [], 2: L.teams[2] || [] } : null,
      captains: L.captains ? { 1: L.captains[1] || '', 2: L.captains[2] || '' } : null,
      coin_captain: L.coin_captain || '',
      stage: L.stage || 'coin',
      coin_side: L.coin_side || null,
      coin_result: L.coin_result || null,
      toss_winner: L.toss_winner || null,
      advantage: L.advantage || null,
      side_picker: L.side_picker || null,
      ban_advantage: L.ban_advantage || null,
      sides: L.sides ? { 1: L.sides[1] || '', 2: L.sides[2] || '' } : null,
      first_ban: L.first_ban || null,
      ban_turn: L.ban_turn || null,
      bans: (L.bans || []).map((b) => ({ team: b.team, map: b.map, auto: Boolean(b.auto) })),
      coin_auto: Boolean(L.coin_auto),
      advantage_auto: Boolean(L.advantage_auto),
      side_auto: Boolean(L.side_auto),
      // Seconds left on THIS stage, for the clock everyone watches. Sent as a countdown rather
      // than a deadline because every other clock in this protocol is (accept_seconds,
      // connect_seconds, lobby_seconds) and a client that ticks one already ticks them all.
      stage_seconds: stageSeconds(L.stage) && L.stage_deadline
        ? Math.max(0, Math.round((L.stage_deadline - Date.now()) / 1000)) : 0,
      stage_total_seconds: stageSeconds(L.stage),
      // The old names for the same clock, veto-only as they always were. A hub on the previous
      // release reads these and nothing else, and the version gate ships LENIENT, so it is still
      // out there; it loses the three new clocks but keeps the one it already drew.
      ban_seconds: L.stage === 'veto' && L.stage_deadline
        ? Math.max(0, Math.round((L.stage_deadline - Date.now()) / 1000)) : 0,
      ban_total_seconds: BAN_SECONDS,
      pool: (L.pool || []).slice(),
      map: L.map || null,
    };
  }

  function broadcastLobby(match) {
    const payload = lobbyPayload(match);
    for (const p of match.players) sendTo(p.player_id, { type: 'lobby', match_id: match.id, ...payload });
  }

  function lobbyTeamOf(match, steamId) {
    const L = match.lobby;
    if (!L) return 0;
    if ((L.teams[1] || []).includes(steamId)) return 1;
    if ((L.teams[2] || []).includes(steamId)) return 2;
    return 0;
  }

  function isLobbyCaptain(match, steamId, team) {
    return Boolean(match.lobby && team && match.lobby.captains[team] === steamId);
  }

  function lobbyRemaining(match) {
    const L = match.lobby;
    const banned = new Set((L.bans || []).map((b) => b.map));
    return (L.pool || []).filter((m) => !banned.has(m));
  }

  /** The designated captain calls heads/tails; the SERVER decides the face once and works out
   * the winning team. The captain is team 1's, so the call is FOR team 1. */
  function flipCoin(steamId, body) {
    const match = matches.get(inMatch.get(steamId));
    if (!match || match.state !== 'ready' || !match.lobby) return { ok: false, error: 'No lobby to flip in.' };
    const L = match.lobby;
    if (L.stage !== 'coin') return { ok: false, error: 'The coin has already been flipped.' };
    if (steamId !== L.coin_captain) return { ok: false, error: 'Only the match captain flips the coin.' };
    const side = body && (body.side === 'heads' || body.side === 'tails') ? body.side : null;
    if (!side) return { ok: false, error: 'Call heads or tails.' };
    applyCoin(match, side, false);
    return { ok: true, ...lobbyPayload(match) };
  }

  /** The one place a coin call is recorded, whoever made it. The face is decided HERE, at the
   * moment of the call, and then the lobby sits in `flipping` for FLIP_SECONDS so all ten clients
   * spin the same coin and watch it land on the same side. */
  function applyCoin(match, side, auto) {
    const L = match.lobby;
    L.coin_side = side;
    L.coin_auto = Boolean(auto);
    L.coin_result = (crypto.randomBytes(1)[0] & 1) ? 'heads' : 'tails';
    L.toss_winner = (L.coin_result === side) ? 1 : 2;   // captain is team 1's
    L.stage = 'flipping';
    armStageTurn(match);
    broadcastLobby(match);
  }

  /** The coin came down. Nothing is decided here - toss_winner was settled when it went up - so
   * this only opens the next stage and puts it on its clock. */
  function landCoin(match) {
    const L = match.lobby;
    if (L.stage !== 'flipping') return;
    L.stage = duel ? 'side' : 'choice';
    if (duel) { L.side_picker = L.toss_winner; L.advantage = 'side'; L.map = mode.fixedMap; }
    armStageTurn(match);
    broadcastLobby(match);
  }

  /** The toss winner takes ONE advantage: the starting SIDE (and gets the attack/defend
   * selector) or the last BAN. The other team gets whatever is left. The ban ORDER is set so
   * the advantage team bans LAST whatever the pool size. */
  function chooseAdvantage(steamId, body) {
    if (duel) return {ok:false,error:'This mode has no advantage choice.'};
    const match = matches.get(inMatch.get(steamId));
    if (!match || match.state !== 'ready' || !match.lobby) return { ok: false, error: 'No lobby.' };
    const L = match.lobby;
    if (L.stage !== 'choice') return { ok: false, error: 'The advantage has already been chosen.' };
    const winner = L.toss_winner;
    if (!isLobbyCaptain(match, steamId, winner)) return { ok: false, error: 'Only the toss winner chooses.' };
    const kind = body && (body.kind === 'side' || body.kind === 'ban') ? body.kind : null;
    if (!kind) return { ok: false, error: 'Choose side or ban.' };
    applyAdvantage(match, kind, false);
    return { ok: true, ...lobbyPayload(match) };
  }

  /** The one place the advantage is recorded, whoever decided it. */
  function applyAdvantage(match, kind, auto) {
    const L = match.lobby;
    const winner = L.toss_winner;
    const loser = winner === 1 ? 2 : 1;
    L.advantage = kind;
    L.advantage_auto = Boolean(auto);
    if (kind === 'side') { L.side_picker = winner; L.ban_advantage = loser; }
    else { L.side_picker = loser; L.ban_advantage = winner; }
    L.first_ban = banFirstTeam((L.pool || []).length, L.ban_advantage);
    L.ban_turn = L.first_ban;
    L.stage = 'side';                 // the side picker selects attack/defend before the veto
    armStageTurn(match);
    broadcastLobby(match);
  }

  /** The team holding the side advantage picks attack or defend; the other gets the opposite. */
  function pickSide(steamId, body) {
    const match = matches.get(inMatch.get(steamId));
    if (!match || match.state !== 'ready' || !match.lobby) return { ok: false, error: 'No lobby.' };
    const L = match.lobby;
    if (L.stage !== 'side') return { ok: false, error: 'It is not time to pick a side.' };
    if (!isLobbyCaptain(match, steamId, L.side_picker)) return { ok: false, error: 'That is not your pick.' };
    const side = body && (body.side === 'attack' || body.side === 'defend') ? body.side : null;
    if (!side) return { ok: false, error: 'Pick attack or defend.' };
    applySide(match, side, false);
    return { ok: true, ...lobbyPayload(match) };
  }

  /** The one place the starting sides are recorded, whoever decided them. */
  function applySide(match, side, auto) {
    const L = match.lobby;
    const other = L.side_picker === 1 ? 2 : 1;
    L.sides = { [L.side_picker]: side, [other]: side === 'attack' ? 'defend' : 'attack' };
    L.side_auto = Boolean(auto);
    L.stage = duel ? 'ready' : 'veto';
    if (duel) { L.map = mode.fixedMap; match.map = mode.fixedMap; clearStageTurn(match); }
    else armStageTurn(match);         // the first turn is on the clock like every other stage
    broadcastLobby(match);
  }

  /** One veto ban, by the captain whose turn it is. When one map is left it is the match map and
   * the lobby is decided (stage 'ready'); a client then opens the connect window. */
  function banMap(steamId, body) {
    if (duel) return {ok:false,error:'This mode has no map bans.'};
    const match = matches.get(inMatch.get(steamId));
    if (!match || match.state !== 'ready' || !match.lobby) return { ok: false, error: 'No lobby.' };
    const L = match.lobby;
    if (L.stage !== 'veto') return { ok: false, error: 'It is not the veto yet.' };
    if (!isLobbyCaptain(match, steamId, L.ban_turn)) return { ok: false, error: 'It is not your turn to ban.' };
    const name = body && typeof body.map === 'string' ? body.map.trim() : '';
    const remaining = lobbyRemaining(match);
    if (!remaining.includes(name)) return { ok: false, error: 'That map cannot be banned.' };
    applyBan(match, name, false);
    return { ok: true, ...lobbyPayload(match) };
  }

  // ---------------------------------------------------------------- chat
  //
  // Two channels, both of them relayed here rather than echoed locally by the hub, because a hub
  // that only shows you your own messages is what this replaced. `team` reaches your side of the
  // lobby, `all` reaches all ten.
  //
  // THREE THINGS HAPPEN BEFORE A LINE IS SENT ON, and each is here rather than in the hub for the
  // same reason: the hub runs on the sender's machine.
  //
  //   1. It is FILTERED (server/censor.cjs). The hub filters as you type so the word never lands
  //      on your own screen, but a patched client simply does not call that. This is the copy an
  //      attacker cannot reach, and it is the one whose output everybody actually sees.
  //   2. The audience is worked out from the SERVER's idea of the teams, not from a channel name
  //      the client picked. Asking for `team` does not let you into a team you are not on.
  //   3. The sender's name is resolved PER RECIPIENT, so an anonymised enemy's persona is never
  //      put on the wire at all (chatNameFor).

  /** {player_id: call sign} for the team `viewerId` is NOT on, or {} when nothing is hidden. */
  function chatCallsigns(match, viewerId) {
    // Only while the pre-round lobby is the screen. From the connect window on, leaving is a
    // no-show that costs elo and a queue ban, so the penalty is the deterrent and the names cost
    // nothing - the same boundary hub/competitive.py hide_enemies() draws.
    if (!match.lobby || match.state !== 'ready') return {};
    const mine = lobbyTeamOf(match, viewerId);
    if (!mine) return {};
    const theirs = (match.lobby.teams[mine === 1 ? 2 : 1] || []).slice().sort();
    const out = {};
    theirs.forEach((id, i) => {
      out[id] = i < ENEMY_CALLSIGNS.length ? ENEMY_CALLSIGNS[i] : `Contact ${i + 1}`;
    });
    return out;
  }

  /** What `viewerId` may be told the sender is called. */
  function chatNameFor(match, senderId, viewerId) {
    const hidden = chatCallsigns(match, viewerId)[senderId];
    if (hidden) return hidden;
    const player = match.players.find((p) => p.player_id === senderId);
    return (player && player.persona) || '';
  }

  /** Persist the transcript, with the TTL the retention rule above sets. Fire and forget: a chat
   *  line that failed to archive must not fail the chat line. */
  function saveChat(match) {
    if (!store || !match.chat || !match.chat.length) return;
    const record = { match_id: match.id, lines: match.chat.slice(-CHAT_KEEP) };
    Promise.resolve(store(['SET', chatKey(match.id), JSON.stringify(record),
                           'EX', String(CHAT_TTL_SECONDS)])).catch(() => {});
  }

  /** A match was reported out of, so its transcript has to outlive the ordinary chat TTL and last
   *  as long as the report itself. EXPIRE only ever lengthens it here: REPORT_TTL is the larger of
   *  the two by default, and if it were ever configured smaller this would be skipped rather than
   *  cutting an existing log short. */
  function keepChatForReport(matchId) {
    if (!store || !matchId || REPORT_TTL_SECONDS <= CHAT_TTL_SECONDS) return;
    Promise.resolve(store(['EXPIRE', chatKey(matchId), String(REPORT_TTL_SECONDS)])).catch(() => {});
  }

  /** One match's transcript, for the admin console. null when it has expired or never existed. */
  async function readChat(matchId) {
    if (!store) return null;
    try {
      const raw = await store(['GET', chatKey(String(matchId || ''))]);
      return raw ? identity.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function sendChat(steamId, body) {
    const match = matches.get(inMatch.get(steamId));
    if (!match) return { ok: false, error: 'No match.' };
    if (match.state === 'over' || match.state === 'cancelled') return { ok: false, error: 'That match is over.' };

    const wanted = String((body && body.channel) || 'team');
    // Unknown channel falls back to TEAM, never all: the cost of getting that backwards is a
    // message meant for four people reaching ten. Same rule as the hub's chat_log().
    const channel = wanted === 'all' ? 'all' : 'team';
    const text = String((body && body.text) || '').trim().slice(0, CHAT_MAX_CHARS);
    if (!text) return { ok: false, error: 'Nothing to say.' };

    const now = Date.now();
    const last = match.chat_at && match.chat_at.get(steamId);
    if (last && now - last < CHAT_MIN_GAP_MS) return { ok: false, error: 'Slow down.' };

    const team = lobbyTeamOf(match, steamId);
    if (channel === 'team' && !team) return { ok: false, error: 'You are not on a team yet.' };

    const clean = censorLib.censor(text);
    if (!match.chat) match.chat = [];
    if (!match.chat_at) match.chat_at = new Map();
    match.chat_at.set(steamId, now);
    match.chat.push({ at: now, by: steamId, channel, team, text: clean });
    if (match.chat.length > CHAT_KEEP) match.chat.splice(0, match.chat.length - CHAT_KEEP);
    saveChat(match);

    // The audience, from the server's teams. `team` is everyone the server puts on the sender's
    // side - which is also why a client cannot talk into the other team's channel by asking to.
    const audience = channel === 'all'
      ? match.players
      : match.players.filter((p) => lobbyTeamOf(match, p.player_id) === team);
    for (const p of audience) {
      sendTo(p.player_id, {
        type: 'chat', match_id: match.id, channel,
        player_id: steamId,
        name: chatNameFor(match, steamId, p.player_id),
        text: clean, at: now,
      });
    }
    return { ok: true };
  }

  // ---------------------------------------------------------------- connect window
  function connectPayload(match, forSteamId) {
    ensureReportCredential(match);
    return {
      type: 'match_connecting', match_id: match.id,
      stamped: Boolean(match.lobby_stamped), teams: match.teams || (match.lobby && match.lobby.teams),
      sides: match.sides || (match.lobby && match.lobby.sides),
      network: match.network || null,
      connect_seconds: Math.max(0, Math.round((match.deadline - Date.now()) / 1000)),
      map: match.map, host: match.host, host_game_steam_id: identity.gameFor(match, match.host),
      // The roster, for the same reason livePayload carries it: this is the whole of what a
      // hub that reconnects into the connect window gets, and without it that hub has no
      // names - so it cannot even say who is hosting, and would fall back to the player
      // themselves. Everyone gets it, not only the rejoiner: one payload, one shape.
      players: match.players.map((p) => ({ player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id), persona: p.persona, avatar: p.avatar || '' })),
      connected: match.players.filter((p) => p.connected).map((p) => p.player_id),
      total: match.players.length,
      // per-player: what walking away from THIS window would cost the person being told
      no_show_elo: NO_SHOW_RR,
      no_show_rr: NO_SHOW_RR,
      no_show_seconds: forSteamId ? nextNoShowSeconds(forSteamId) : rungSeconds(1),
      session_key:require('./recovery.cjs').sessionFor(match),
      ...(forSteamId === match.host ? { report_token: match.reportToken } : {}),
      ...(match.migration_capabilities?.[forSteamId] ? {migration_token:match.migration_capabilities[forSteamId]} : {}),
      host_epoch:match.host_epoch||0,
    };
  }

  function ensureReportCredential(match) {
    if (!match.reportToken) match.reportToken = crypto.randomBytes(32).toString('hex');
    if(!match.migration_capabilities) {
      match.host_epoch=0;
      match.migration_capabilities=Object.fromEntries(match.players.map(p=>[p.player_id,
        p.player_id===match.host?match.reportToken:crypto.randomBytes(32).toString('hex')]));
      match.migration_digests=Object.fromEntries(Object.entries(match.migration_capabilities).map(([id,t])=>[id,require('./migration.cjs').digest(t)]));
    }
    if (!match.agreedScoreLimit) {
      const rules = typeof rankedRules === 'function' ? rankedRules() : null;
      const requested = Number(rules && rules.score_limit);
      match.agreedScoreLimit = (duel ? (match.expected_score_limit ?? mode.scoreLimit) : forcedScoreLimit()) ??
        (Number.isInteger(requested) && requested > 0 && requested <= 999 ? requested : DEFAULT_SCORE_LIMIT);
    }
  }

  function authoriseReport(token) {
    if (typeof token !== 'string' || !/^[a-f0-9]{64}$/.test(token)) return null;
    const incoming = Buffer.from(token, 'hex');
    for (const match of matches.values()) {
      if (!['connecting', 'live'].includes(match.state) || match.finished ||
          !/^[a-f0-9]{64}$/.test(match.reportToken || '')) continue;
      if (crypto.timingSafeEqual(incoming, Buffer.from(match.reportToken, 'hex'))) {
        return { steamId: identity.gameFor(match, match.host), playerId: match.host, matchId: match.id, epoch:match.host_epoch||0, scoreLimit: match.agreedScoreLimit || DEFAULT_SCORE_LIMIT };
      }
    }
    return null;
  }

  async function refreshAuthority(match) {
    if(!store || !match.migration_capabilities)return;
    const raw=await store(['GET',authorityKey(match.id)],{strict:true});
    if(!raw) {await persistLive(match);return;}
    const a=identity.parse(raw);
    if(a.closed)throw Error('Match authority ended');
    if(a.epoch===(match.host_epoch||0) && a.host===match.host &&
       (a.phase||'playing')===(match.recovery?.phase||'playing')&&
       (a.recovery_revision||0)===(match.recovery?.revision||0)&&
       (a.roster_revision||0)===(match.roster_revision||0))return;
    const snapshot=await store(['GET',liveMatchKey(match.id)],{strict:true});
    if(!snapshot)throw Error('Match authority ended');
    const restored=reviveMatch(await combatStorage.unpack(store,identity.parse(snapshot),liveMatchKey(match.id)));
    for(const key of ['timer','collectTimer','stage_timer','ban_timer'])if(match[key])clearTimeout(match[key]);
    for(const key of Object.keys(match))delete match[key];
    Object.assign(match,restored);
    rearm(match);
    releaseRecoveryDepartures(match);
  }

  function releaseRecoveryDepartures(match) {
    for(const p of match.left||[])if(p.disconnect_confirmed&&p.left_state==='live') {
      revokeJoinPermit(p.player_id);
      if((!match.terminal||!duel)&&inMatch.get(p.player_id)===match.id) {
        inMatch.delete(p.player_id);
        sendTo(p.player_id,{type:'match_over',match_id:match.id,reason:'reconnect_timeout',recovery_excluded:p.recovery_excluded===true});
      }
    }
  }

  async function authoriseReportFresh(token) {
    const match=[...matches.values()].find(m=>m.reportToken===token || Object.values(m.migration_capabilities||{}).includes(token));
    if(!match)return null;
    return matchOperation(match.id,async()=>{
      await refreshAuthority(match);
      return authoriseReport(token);
    });
  }

  function withReportAuthority(auth,operation,{allowRestoring=false}={}) {
    return matchOperation(auth.matchId,async()=>{
      const m=matches.get(auth.matchId);
      if(m) {
        await refreshAuthority(m);
        if((m.host!==auth.playerId && identity.gameFor(m,m.host)!==auth.steamId) || (m.host_epoch||0)!==(auth.epoch||0))
          throw Error('Superseded match reporter');
        if(m.recovery?.phase==='restoring'&&!allowRestoring)throw Error('Recovery has not been verified');
      } else if(!auth.completed)throw Error('Match no longer active');
      return reportScope.run({match:auth.matchId,host:auth.playerId,epoch:auth.epoch||0},operation);
    });
  }

  async function recoveryTransition(match,player,token,fields) {
    const result=await require('./recovery.cjs').execute(store,
      [authorityKey(match.id),liveMatchKey(match.id),settlementKey(match.id),`${prefix||'hub:'}live:recovery:${match.id}`],
      {operation:fields.operation,epoch:fields.epoch,session:fields.session,sequence:fields.sequence,
        checkpoint:fields.checkpoint,penalties:fields.penalties,player,token,now:Date.now(),ttl:LIVE_STATE_TTL_SECONDS});
    if(result.changed&&result.snapshot) {
      const restored=reviveMatch(result.snapshot);
      for(const key of ['timer','collectTimer','stage_timer','ban_timer'])if(match[key])clearTimeout(match[key]);
      for(const key of Object.keys(match))delete match[key];
      Object.assign(match,restored);rearm(match);
      hostPermits.delete(player);
      releaseRecoveryDepartures(match);
      for(const p of match.players)sendTo(p.player_id,livePayload(match,p.player_id));
    }
    return result;
  }

  async function syncRecoveryRoster(match) {
    if(match.recovery?.phase!=='restoring'||!match.recovery.rejoin_until)return;
    const fields={epoch:match.host_epoch,session:match.session_key};
    if(!match.recovery.roster)await recoveryTransition(match,match.host,match.reportToken,{...fields,operation:'seal'});
    const roster=match.recovery.roster;
    if(roster&&!roster.done) {
      const saved={};
      if(!duel)for(const id of roster.excluded)saved[id]=await reconnectPenalty(match,id,{recovery:true});
      const result=await recoveryTransition(match,match.host,match.reportToken,{...fields,operation:'adjudicated',penalties:saved});
      if(!result.ok)throw Error('Recovery absences are not saved');
    }
    if(match.terminal?.recovery)return finishDuelDecision(match);
  }

  async function applyRecovery(match,player,token,fields) {
    await syncRecoveryRoster(match);
    const result=await recoveryTransition(match,player,token,fields);
    if(result.ok&&fields.operation==='pulse')await syncRecoveryRoster(match);
    return {ok:result.ok,error:result.error,recovery:result.recovery,
      ...(result.ok?{match:livePayload(match,player)}:{})};
  }

  async function recoveryReport(token,fields) {
    if(!store||!/^[a-f0-9]{16}$/.test(fields?.match_id||'')||
       !['checkpoint','pulse','prepared','restored'].includes(fields.operation))return {ok:false,error:'Invalid recovery report'};
    return matchOperation(fields.match_id,async()=>{
      const match=matches.get(fields.match_id);if(!match)return {ok:false,error:'No match'};
      await refreshAuthority(match);
      const player=Object.entries(match.migration_capabilities||{}).find(([,t])=>t===token)?.[0];
      if(!player||identity.gameFor(match,player)!==fields.user_id)return {ok:false,error:'Invalid participant'};
      return applyRecovery(match,player,token,fields);
    });
  }

  async function recoveryAction(player,fields) {
    if(!store||!/^[a-f0-9]{16}$/.test(fields?.match_id||'')||
       !['status','closed','claim'].includes(fields.operation))return {ok:false,error:'Invalid recovery action'};
    return matchOperation(fields.match_id,async()=>{
      const match=matches.get(fields.match_id);
      if(!match||inMatch.get(player)!==match.id)return {ok:false,error:'No match'};
      await refreshAuthority(match);
      const token=match.migration_capabilities?.[player];
      if(!token)return {ok:false,error:'Invalid participant'};
      return applyRecovery(match,player,token,fields);
    });
  }

  async function migrationReport(token,fields) {
    if(!store || !/^[a-f0-9]{64}$/.test(token||'') || !/^[a-f0-9]{16}$/.test(fields?.match_id||'') ||
       !['endorse','activate'].includes(fields.operation) || !Number.isSafeInteger(fields.epoch) || fields.epoch<0)
      return {ok:false,error:'Invalid handoff'};
    return matchOperation(fields.match_id,async()=>{
      const match=matches.get(fields.match_id);
      if(!match)return {ok:false,error:'Match is not live'};
      await refreshAuthority(match);
      const player=Object.entries(match.migration_capabilities||{}).find(([,t])=>t===token)?.[0];
      if(!player)return {ok:false,error:'Invalid handoff credential'};
      const candidate=fields.candidate?identity.playerFor(match,fields.candidate):'';
      if(fields.candidate&&!candidate)return {ok:false,error:'Invalid successor'};
      const result=await require('./migration.cjs').transition(store,
        [authorityKey(match.id),liveMatchKey(match.id),settlementKey(match.id)],
        {operation:fields.operation,player,token,epoch:fields.epoch,candidate,ttl:LIVE_STATE_TTL_SECONDS});
      if(result.ok && result.snapshot) {
        const restored=reviveMatch(result.snapshot);
        for(const key of ['timer','collectTimer','stage_timer','ban_timer'])if(match[key])clearTimeout(match[key]);
        for(const key of Object.keys(match))delete match[key];
        Object.assign(match,restored);rearm(match);
        for(const p of match.players)sendTo(p.player_id,livePayload(match,p.player_id));
      }
      return {ok:result.ok,error:result.error};
    });
  }

  async function authoriseFinalReport(token, matchId) {
    if (typeof token !== 'string' || !/^[a-f0-9]{64}$/.test(token) || !/^[a-f0-9]{16}$/.test(matchId || '')) return null;
    const active = await authoriseReportFresh(token);
    if (active) return active.matchId === matchId ? active : null;
    const receipt = await readReceipt(matchId);
    if (receipt?.data_collected !== true || !/^[a-f0-9]{64}$/.test(receipt.report_digest || '')) return null;
    const digest = crypto.createHash('sha256').update(token).digest();
    if (!crypto.timingSafeEqual(digest, Buffer.from(receipt.report_digest, 'hex'))) return null;
    const full = identity.freezeMatch(receipt.publicMatch);
    const steamId = identity.gameFor(full, receipt.host);
    return steamId ? {steamId, playerId:receipt.host, matchId, completed:true,epoch:receipt.host_epoch||0, scoreLimit:receipt.limit} : null;
  }

  // Redeploy compatibility for a match that was already active before per-match report
  // credentials existed. Only restoreMatches can create this authority; request data cannot.
  const LEGACY_REPORT_EVENTS = new Set([
    'ch_lobby_read', 'ch_lobby_write', 'ch_bb5_stats', 'ch_bb5_score',
    'ch_bb5_round', 'ch_bb5_state', 'ch_bb5_kill', 'ch_team_verified', 'ch_team_kill',
  ]);
  const LEGACY_MATCH_ID_EVENTS = new Set([
    'ch_bb5_stats', 'ch_bb5_score', 'ch_bb5_round', 'ch_bb5_state', 'ch_bb5_kill',
  ]);
  function authoriseLegacyReport(hostId, event, claimedMatch = '') {
    const host = String(hostId || '');
    event = String(event || '');
    if (!LEGACY_REPORT_EVENTS.has(event)) return null;
    const match = matches.get(inMatch.get(host));
    if (!match || match.legacyReportAuth !== true || match.host !== host || match.finished ||
        !['connecting', 'live'].includes(match.state)) return null;
    const claimed = String(claimedMatch || '');
    if (LEGACY_MATCH_ID_EVENTS.has(event) && /^[0-9a-f]{16}$/.test(claimed) &&
        claimed !== match.id) return null;
    return { steamId: host, matchId: match.id,
      scoreLimit: match.agreedScoreLimit || DEFAULT_SCORE_LIMIT };
  }

  /**
   * Take the lobby's outcome off the connect POST, so the history record can say who was on
   * which team, which side they started, and how the map was arrived at.
   *
   * The coin flip and the veto still run in the hub (moving them server-side is task 2 in
   * memory section 10), so this is CLIENT-REPORTED and is marked as such in the record. It is
   * checked rather than trusted: a steamId that was not in the match is dropped, a player can
   * only appear on one team, and the sizes are bounded. Nothing here can affect the match
   * itself - it is only ever read back as history.
   */
  function adoptLobby(match, body) {
    if (!body || typeof body !== 'object') return;
    const roster = new Set(match.players.map((p) => p.player_id));
    const seen = new Set();
    const teams = {};
    for (const key of ['1', '2']) {
      const given = Array.isArray(body.teams && body.teams[key]) ? body.teams[key] : [];
      const team = [];
      for (const raw of given.slice(0, match.players.length)) {
        const steamId = String(raw || '');
        if (roster.has(steamId) && !seen.has(steamId)) { seen.add(steamId); team.push(steamId); }
      }
      if (team.length) teams[key] = team;
    }
    if (Object.keys(teams).length) { match.teams = teams; match.lobby_reported = true; }

    const sides = {};
    for (const key of ['1', '2']) {
      const side = String((body.sides && body.sides[key]) || '');
      if (side === 'attack' || side === 'defend') sides[key] = side;
    }
    if (Object.keys(sides).length) { match.sides = sides; match.lobby_reported = true; }

    if (Array.isArray(body.bans)) {
      const bans = [];
      for (const entry of body.bans.slice(0, 16)) {
        const team = Number((entry && entry.team) || 0);
        const name = String((entry && entry.map) || '').slice(0, 64);
        if (name) bans.push({ team: team === 1 || team === 2 ? team : 0, map: name });
      }
      if (bans.length) { match.bans = bans; match.lobby_reported = true; }
    }
  }

  /**
   * A LIVE match, as a client that has just (re)connected needs to see it.
   *
   * The lobby runs in the hub, not here, so a hub that was closed, updated or offline has
   * none of it left: no roster, no teams, no sides, no veto, no map. Without this it would
   * come back to the idle screen while a match it is still in runs on without it. Everything
   * the server does know goes out; whatever it does not (levels, pings) the hub fills in.
   */
  function livePayload(match, recipient) {
    return {
      type: 'match_live', match_id: match.id, map: match.map, host: match.host, host_game_steam_id: identity.gameFor(match, match.host),
      roster_revision:match.roster_revision||0,
      vote: voidVotePayload(match, recipient),
      network: match.network || null,
      live_seconds: Math.max(0, Math.round((match.deadline - Date.now()) / 1000)),
      players: match.players.map((p) => ({ player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id), persona: p.persona, avatar: p.avatar || '' })),
      teams: match.teams || null,
      sides: match.sides || null,
      bans: match.bans || null,
      reconnect_waiting: Object.entries(match.reconnect || {}).map(([player_id,w])=>({player_id,deadline:w.deadline})),
      ...(recipient === match.host && match.reportToken ? {report_token:match.reportToken} : {}),
      ...(match.migration_capabilities?.[recipient] ? {migration_token:match.migration_capabilities[recipient]} : {}),
      host_epoch:match.host_epoch||0,
      session_key:require('./recovery.cjs').sessionFor(match),
      ...(match.recovery?{recovery:{...match.recovery,server_now:Date.now(),world_ready:Boolean(match.lobby_stamped),
        can_rejoin:require('./recovery.cjs').canRejoin(match,recipient,Date.now()),
        ...(match.recovery.phase==='restoring'?{checkpoint:match.recovery_checkpoint}:{})}}:{}),
    };
  }

  /**
   * The veto is over and a host has been picked: start the three minutes.
   *
   * Any player in the match may open the window - the server's veto ends for everyone at the
   * same moment (stage 'ready'), so whoever's POST lands first wins and the rest are no-ops.
   */
  function beginConnect(steamId, body) {
    const matchId = inMatch.get(steamId);
    const match = matchId && matches.get(matchId);
    if (!match) return { ok: false, error: 'No match to start.' };
    if (match.state === 'connecting') {
      return { ok: true, already: true, ...connectPayload(match, steamId) };
    }
    if (match.state !== 'ready') return { ok: false, error: 'That match is not ready.' };

    // B-04, 2026-09-14. This body used to be copied in with no checking beyond a length slice.
    // Two things went wrong with that. A body that is not a plain object - an ARRAY, say -
    // made `body.map` find Array.prototype.map, so the match started on a map literally called
    // "function map() { [native code] }". And `host` could be any SteamID at all, including one
    // belonging to nobody in this match, which all ten clients then tried to connect to.
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return { ok: false, error: 'Send the map and the host.' };
    }
    // `host` is `let` because COMP_PREFER_HOST may reassign it below. The map is NOT read from the
    // body here: the server-authoritative lobby decides it (the `let map` block further down), which
    // also keeps the old-client fallback that still validates a body.map with MAP_NAME.
    let host = match.network ? match.network.host : (typeof body.host === 'string' ? body.host.trim() : '');
    if (!match.players.some((p) => p.player_id === host)) {
      return { ok: false, error: 'The host has to be a player in this match.' };
    }
    // A TEST LEVER, off unless an operator sets it. The client nominates the lowest-ping player
    // (competitive.py: min(players, key=ping)), which is right for a real match and useless when you
    // need a SPECIFIC machine to host - the one running the build under test. Without this, proving
    // a new lobby pak needs the dice to land on the right person.
    //
    // It only reorders the choice among players who are ALREADY in this match: the id must still be
    // on the roster, so it grants nothing and lets nobody in who was not there. Unset it and the
    // client's pick stands.
    const prefer = String(process.env.COMP_PREFER_HOST || '').trim();
    if (!match.network && prefer && prefer !== host && match.players.some((p) => p.player_id === prefer)) {
      console.log('[match] COMP_PREFER_HOST: host %s -> %s', host, prefer);
      host = prefer;
    }

    // The lobby ran on the SERVER, so its decisions - the map, the teams, the sides, the veto -
    // are the truth, not the body. This closes the hole the old comment lamented: a modified hub
    // can no longer open the window early on a map of its choosing, because the map is whatever
    // the server's veto left standing and the veto is only 'ready' once it really ran.
    let map;
    if (match.lobby && match.lobby.stage === 'ready' && match.lobby.map) {
      map = match.lobby.map;
      match.teams = { 1: match.lobby.teams[1], 2: match.lobby.teams[2] };
      match.sides = { 1: match.lobby.sides[1], 2: match.lobby.sides[2] };
      match.bans = match.lobby.bans.slice();
      match.lobby_server = true;      // recorded as server-authoritative, not client-reported
    } else if (match.lobby) {
      // The lobby exists but has not finished: refuse rather than start on an undecided map.
      return { ok: false, error: 'The lobby is not decided yet.' };
    } else {
      // No server lobby (an old client path): fall back to the client-reported body.
      map = typeof body.map === 'string' ? body.map.trim() : '';
      if (!MAP_NAME.test(map)) return { ok: false, error: 'That is not a map name.' };
      adoptLobby(match, body);
    }

    const expected = duel && match.expected_score_limit && match.expected_max_rounds
      ? {score_limit:match.expected_score_limit,max_rounds:match.expected_max_rounds} : rulesExpected();
    if (!expected) return { ok: false, error: 'Published game rules are unavailable.' };
    match.expected_score_limit = expected.score_limit;
    match.expected_max_rounds = expected.max_rounds;
    match.assigned_teams = { 1: [...(match.teams?.[1] || [])], 2: [...(match.teams?.[2] || [])] };
    match.state = 'connecting';
    match.map = map;
    match.host = host;
    match.initial_host ||= host;
    match.starting_sides ||= structuredClone(match.sides||{});
    ensureReportCredential(match);
    match.openedBy = steamId;         // who opened the window, for attribution
    for (const p of match.players) p.connected = false;
    // Recorded BEFORE the arm so a second beginConnect - which re-runs the whole body, including
    // resetting every player's `connected` - is visible as two rows rather than one.
    note(match, 'beginConnect', { by: steamId, host: String(match.host || ''), map: String(map || ''),
                                  age: Math.round((Date.now() - (match.created || 0)) / 1000) });
    // The ONLY place auto-host is ever permitted: this match, this host, once. Everything else -
    // a normal launch, a second lobby load after the match - gets no reply and no travel.
    grantHostPermit(match.host, CONNECT_SECONDS + 120);
    arm(match, CONNECT_SECONDS, 'connect', () => expireConnect(match.id));
    for (const p of match.players) sendTo(p.player_id, connectPayload(match, p.player_id));
    return { ok: true, ...connectPayload(match, steamId) };
  }

  /**
   * "I am in the game." Today the hub says this on the player's behalf; when the gamemode can
   * report its own roster (the lobby probe, task 14) that becomes the authority and this
   * route stays as the fallback for a player whose game never launched.
   */
  /** Tell everyone in the match who is in, whether the joiners are released, and how long is left.
   *
   * `stamped` is NOT the same fact as `connected`, and the joiner's whole timing depends on the
   * difference. `connected` is anyone's hub saying they are in; `stamped` is the HOST's game
   * reporting from inside the match world that its lobby is up, which is the only moment a
   * joiner's single lobby search can find anything. See gameReportedIn for what sets it.
   *
   * IT IS A FUNCTION BECAUSE IT HAS TWO CALLERS, and having only one was the bug. match_connect is
   * the only message that carries `stamped`, and it used to be built solely inside reportConnected
   * - which gameReportedIn skips once the host is already connected. Since ch_lobby_read (t+12s)
   * always marks the host connected BEFORE ch_lobby_write (t+30s) sets the stamp, the stamped
   * payload was never sent to anyone, ever: the joiners were released on the server and left
   * staring at a Launch button the hub greys on that very field.
   *
   * The payload is a full snapshot rather than a delta, so sending it more than once is harmless. */
  function broadcastConnectProgress(match) {
    const connected = match.players.filter((p) => p.connected);
    const progress = { type: 'match_connect', match_id: match.id,
                       stamped: Boolean(match.lobby_stamped),
                       connected: connected.map((p) => p.player_id), total: match.players.length };
    // HOW LONG IS ACTUALLY LEFT, and it rides on every progress message rather than only on the
    // one that opened the window (Sam, 2026-09-16: "the time to connect timer is still ticking
    // down and the timer could run out before the hoster respawns"). The deadline is re-armed when
    // the host reports in and when their game asks for its travel permit - but until now nobody
    // was told, so ten hubs kept counting down from the number beginConnect handed them and a host
    // with a slow boot left the match watching a clock hit 0:00 that the server had already reset.
    // Only when the connect clock is the one running: after everyone is in, the deadline belongs
    // to the teams gate and means something else entirely.
    if (match.expiry === 'connect' && match.deadline) {
      progress.connect_seconds = Math.max(0, Math.round((match.deadline - Date.now()) / 1000));
    }
    for (const p of match.players) sendTo(p.player_id, progress);
    return progress;
  }

  function reportConnected(steamId) {
    const matchId = inMatch.get(steamId);
    const match = matchId && matches.get(matchId);
    if (!match || match.state !== 'connecting') return { ok: false, error: 'No match is connecting.' };
    const player = match.players.find((p) => p.player_id === steamId);
    if (!player) return { ok: false, error: 'You are not in that match.' };
    const firstArrival = !player.connected;
    player.connected = true;
    note(match, 'connected', { who: steamId, host: String(match.host || '') === steamId,
                               age: Math.round((Date.now() - (match.created || 0)) / 1000) });

    // HOST-READY re-base. A non-host's join clock only becomes real here: the host reporting in is
    // what lets a joiner's single lobby search find anything (see gameReportedIn). So when the HOST
    // is the one reporting - and the match is not already complete - re-arm the connect deadline to
    // a fresh window, measured from now. A joiner still loading in then always has the full window
    // and is never no-show-penalized early; a genuine no-show who never turns up still pays when
    // this re-armed deadline fires. If the host never reports, the beginConnect deadline stands and
    // expireConnect penalizes the missing host exactly as before.
    if (firstArrival && String(match.host || '') === steamId &&
        match.players.some((p) => !p.connected)) {
      arm(match, CONNECT_SECONDS, 'connect', () => expireConnect(match.id));
    }

    const connected = match.players.filter((p) => p.connected);
    broadcastConnectProgress(match);

    // Everyone being in is necessary and no longer sufficient: goLiveIfReady also asks whether the
    // game's own teams match the lobby's. See TEAMS_GATE_SECONDS.
    const started = goLiveIfReady(match);
    return { ok: true, connected: connected.length, total: match.players.length,
             started: Boolean(started && started.started),
             waiting_for_teams: Boolean(started && started.gated && !started.started) };
  }

  // ---------------------------------------------------------------- the travel permit
  //
  // WHY THE SERVER HOLDS THE KEY TO AUTO-HOST. The lobby pak has no clock of its own - no timer, no
  // Delay, no Tick - so it gets its wait by asking /api/probe/slow and travelling when the reply
  // arrives. That makes the REPLY the permission: no answer, no travel. Nothing in the pak has to
  // change for the hub to gate it.
  //
  // This exists because the pak was, until now, unconditional. GM_CHLobby's BeginPlay runs on EVERY
  // load of the lobby world, so a player who simply started the game was hosted into the match map,
  // and a player who left a match was hosted straight back into it - the lobby world loads, BeginPlay
  // fires, four seconds later they are in Hospital again. Removing the pak file does not help a
  // running game either: paks are mounted at process start, so the file can be deleted and the
  // mounted copy keeps working until the game restarts.
  //
  // A permit is short-lived and tied to ONE match: it is issued when that match opens its connect
  // window, revoked the moment the host reports in from inside the match world, and expires on its
  // own if they never arrive. Anything not covered by one gets no reply and behaves exactly like a
  // stock game.
  //
  // IT IS NOT SPENT BY THE ASKING, and that distinction cost a match on 2026-09-16. It used to be
  // deleted the instant /api/probe/slow was asked, before the held reply had been delivered - so
  // when a reply dropped (the chlobby-12/13/26 failure mode, which the lobby pak has hit before)
  // the pak had asked its one question, got no answer, travelled nowhere, and could never ask
  // again. The match ran its connect window out and cancelled with nobody at fault. The log read:
  //
  //     ch_host_wait -> "travel clock granted to 765...933" -> nothing, ever
  //
  // Spending it on the ASK never protected anything that revoking it on ARRIVAL does not already
  // protect. What must not happen is a player being dragged back into a match they have left, and
  // gameReportedIn revokes the permit the moment they are in - "they are in; a later lobby load
  // must not travel them back". Before arrival, a second lobby load being answered is not a bug,
  // it is the retry this had no way to do. The count is naturally bounded anyway: GM_CHLobby's
  // BeginPlay asks once per load of the lobby world, so the asks cannot outnumber the loads.
  const hostPermits = new Map();     // steamId -> {until, matchId}
  const HOST_PERMIT_SECONDS = 300;   // a cold launch is ~40-60 s; this is slack, not a target

  function grantHostPermit(steamId, seconds) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return false;
    const until = Date.now() + (Number(seconds) || HOST_PERMIT_SECONDS) * 1000;
    const match = matches.get(inMatch.get(id));
    if (!match || match.host !== id || match.state !== 'connecting' || match.lobby_stamped) return false;
    hostPermits.set(id, {until, matchId:match.id});
    match.host_permit_until = until;
    return true;
  }

  function takeHostPermit(steamId) {
    const id = String(steamId || '');
    const match = matches.get(inMatch.get(id));
    const permit = hostPermits.get(id);
    const until = permit ? permit.until : match && match.host_permit_until;
    if (permit && !(until > Date.now())) hostPermits.delete(id);
    return Boolean(match && match.host === id && (match.state === 'connecting'||match.recovery?.phase==='restoring')
      && !match.lobby_stamped && until > Date.now() && (!permit || permit.matchId === match.id));
  }

  function revokeHostPermit(steamId) {
    const id = String(steamId || '');
    const match = matches.get(inMatch.get(id));
    if (match) match.host_permit_until = 0;
    return hostPermits.delete(id);
  }

  /**
   * THE HOST'S GAME ASKING FOR ITS TRAVEL PERMIT - it is booted, standing in the lobby world, and
   * about to open the match map. Called from the /api/probe/slow handler when the permit is
   * granted.
   *
   * IT IS NOT AN ARRIVAL and deliberately does nothing an arrival does: nobody is marked connected
   * and no joiner is released, because at this instant the match world does not exist yet. All it
   * does is stop the connect clock running out underneath a host who is demonstrably on their way.
   * A cold launch is 40-60 s, a Steam update or a shader build is minutes, and every one of those
   * seconds used to come out of the same five minutes the joiners then had to launch inside.
   *
   * EXTEND ONLY. A re-arm that shortened the window would be a new way to lose a match, so this
   * refuses when the deadline already sits further out than a fresh window would.
   */
  function noteHostLaunching(steamId) {
    const id = String(steamId || '');
    const matchId = inMatch.get(id);
    const match = matchId && matches.get(matchId);
    if (!match || match.state !== 'connecting') return false;
    if (String(match.host || '') !== id) return false;
    if (match.expiry !== 'connect') return false;      // the teams gate owns the clock now
    if (!match.players.some((p) => !p.connected)) return false;   // nobody left to wait for
    if ((match.deadline || 0) >= Date.now() + CONNECT_SECONDS * 1000) return false;
    arm(match, CONNECT_SECONDS, 'connect', () => expireConnect(match.id));
    note(match, 'hostLaunching', { host: id });
    broadcastConnectProgress(match);
    return true;
  }

  // ---------------------------------------------------------------- the joiner's permit
  //
  // Same mechanism, opposite population, and issued at a different MOMENT - which is the point.
  // A joiner gets exactly one lobby search per launch (BeginPlay fires once per level load and the
  // lobby world has no clock to retry with), so firing it before the host's lobby exists spends it
  // on an empty Steam, silently. The permit is therefore granted only once the HOST's own game has
  // reported in from inside the match world, so by the time a joiner asks, there is something to
  // find.
  //
  // The hub already waits for the same signal before it even launches the joiners
  // (competitive.py, _maybe_launch_game("host-ready") on the match_connect that carries the host),
  // so in practice the permit is waiting before the game finishes loading. This is the second lock
  // on the same door: if a joiner's game is somehow already at BeginPlay, it still gets no answer
  // until the host is really there.
  const joinPermits = new Map();     // playerId -> {until, matchId}
  const JOIN_PERMIT_SECONDS = 600;   // a match lasts longer than this matters for; one-shot anyway

  /** Every player in the match EXCEPT the host, the moment the host is confirmed in. */
  function grantJoinPermits(match) {
    if (!match || !Array.isArray(match.players)) return 0;
    const until = Math.min(Date.now() + JOIN_PERMIT_SECONDS * 1000,
      match.recovery?.phase==='restoring'&&!match.recovery?.roster ? match.recovery.rejoin_until||Infinity : Infinity);
    let n = 0;
    for (const p of match.players) {
      const id = String(p.player_id || '');
      if (!identity.validPlayer(id) || id === String(match.host || '')||match.recovery?.roster?.excluded.includes(id)) continue;
      joinPermits.set(id, {until, matchId:match.id});
      n += 1;
    }
    return n;
  }

  /** Spend it. One-shot, like the host's: a second lobby load must not re-join. */
  function takeJoinPermit(steamId) {
    const id = String(steamId || '');
    const permit = joinPermits.get(id);
    if (!permit) return false;
    joinPermits.delete(id);
    const match = matches.get(permit.matchId);
    return permit.until > Date.now() && inMatch.get(id) === permit.matchId &&
      !!match && ['connecting','live'].includes(match.state) && match.players.some(p=>p.player_id===id)&&
      !match.recovery?.roster?.excluded.includes(id)&&
      !(match.recovery?.phase==='restoring'&&!match.recovery.roster&&Date.now()>=match.recovery.rejoin_until);
  }

  function revokeJoinPermit(steamId) {
    return joinPermits.delete(String(steamId || ''));
  }

  /**
   * THE HOST'S GAME ASKING WHAT TO DO WITH A PLAYER WHO JUST WALKED IN.
   *
   * The whole point of the branch this lands on: the teams the hub shows and the teams the players
   * stand on must be the SAME teams. They are decided exactly once - matchmaker.bestSplit at
   * formation - read back by buildLobby, and shown by the hub. Nothing re-derives them, and this is
   * how the third reader, the game itself, gets the same answer as the other two.
   *
   * WHY IT ANSWERS IN ONE BIT. `SendAttributionEvent`'s response delegate is
   * DECLARE_DYNAMIC_DELEGATE_OneParam(..., bool) - one bool, and `bSuccess` tracks the HTTP status
   * (measured: chlobby-3 and chlobby-5, 200 -> true and 404 -> false). That is the entire inbound
   * channel from us to a running Bodycam, so a question with three answers has to be asked as two
   * questions with two. Hence `ask`:
   *
   *   'side-one' true authorizes hub side 1 (game TeamID 0)
   *   'side-two' true authorizes hub side 2 (game TeamID 1)
   *   'stranger' true authorizes removing an outsider
   *
   * Each action needs its own positive answer. A refused request, network failure, or HTTP
   * error is never an instruction to assign the other side or kick. Legacy member/side asks
   * remain available for older paks, but new callers never interpret a false as a team.
   *
   * WHO MAY ASK. Only the host of a match that is `connecting` or `live`, and only about a steam id
   * in that same match. Same trust window as gameReportedIn and for the same reason: /api/probe is
   * unauthenticated, so the answer is narrowed to something an outsider could not usefully abuse -
   * here, learning which of two teams a player they already named is on, in a match they already
   * know the host of.
   */
  function teamRuling(hostId, subjectId, ask) {
    const host = String(hostId || '');
    const subject = String(subjectId || '');
    if (!identity.validPlayer(host)) return { ok: false, error: 'not a steamid64' };
    if (!identity.validPlayer(subject)) return { ok: false, error: 'subject is not a steamid64' };
    const matchId = inMatch.get(host);
    const match = matchId && matches.get(matchId);
    if (!match) return { ok: false, error: 'no match' };
    if (match.state !== 'connecting' && match.state !== 'live') {
      return { ok: false, error: 'match is not connecting or live' };
    }
    if (String(match.host || '') !== host) return { ok: false, error: 'not the host' };

    // The roster is `players` - who is STILL in the match. Someone who walked out is deliberately
    // not a member any more: if they reconnect into the game world they are as much a stranger as
    // anyone else, and the kick is the right answer.
    const member = match.players.some((p) => p.player_id === subject)&&!match.recovery?.roster?.excluded.includes(subject);
    if (String(ask || '') === 'member') {
      return { ok: true, match_id: match.id, subject, ask: 'member', member, yes: member };
    }

    // The kick question. Everything above this line has already established that we are looking at
    // a real match in a real state with the real host asking - which is exactly what makes a `true`
    // here mean something a `false` from `member` does not.
    if (String(ask || '') === 'stranger') {
      return { ok: true, match_id: match.id, subject, ask: 'stranger', member, yes: !member };
    }
    if (!['side', 'side-one', 'side-two'].includes(String(ask || ''))) return { ok: false, error: 'unknown ask' };
    if (!member) return { ok: false, error: 'not on the roster' };

    // The lobby's teams, not a fresh split. `match.teams` is written by beginConnect straight off
    // `match.lobby.teams`, which buildLobby read off the matchmaker - the same object the hub was
    // sent in lobbyPayload. If it is somehow absent the honest answer is a refusal: putting a
    // player on a guessed team is exactly the divergence this exists to prevent.
    const teams = match.teams || (match.lobby && match.lobby.teams) || null;
    if (!teams) return { ok: false, error: 'the teams are not decided' };
    const onOne = (teams[1] || []).includes(subject);
    const onTwo = (teams[2] || []).includes(subject);
    if (onOne === onTwo) return { ok: false, error: 'player must belong to exactly one team' };
    // Each side has a positive confirmation; failures never authorize the other side.
    return { ok: true, match_id: match.id, subject, ask,
             team: onOne ? 1 : 2, yes: ask === 'side-two' ? onTwo : onOne };
  }

  /**
   * Put a match live. The one place that does it, so every caller gets the same deadline, the same
   * archive write and the same broadcast.
   */
  function goLive(match, verdict) {
    match.state = 'live';
    // WHEN THE PLAYING STARTED, which is not when the match was formed: the accept window, the
    // lobby and the connect window are all before this, and counting them as time played would
    // hand twenty minutes to an account that never turned up.
    match.live_at = Date.now();
    match.teams_verdict = verdict || null;
    // NOT `match.timer = null`. Every state a player cannot leave by themselves carries a
    // deadline (the contract at the top of this file), and `live` was the one phase missing
    // it - which made it the only phase that could hang for ever.
    arm(match, LIVE_SECONDS, 'live', () => expireLive(match.id));
    // Archived the moment it goes live, not when it ends: a live match ends one player at a
    // time (handleMatchLeave) and may never be closed cleanly at all, so waiting for a tidy
    // ending would lose the matches that actually got played. The score and the winner are
    // filled in later, by whatever reports the scoreboard.
    if (!match.start_ready_verified) archiveMatch(match, { outcome: 'played', reason: '' });
    for (const p of match.players) sendTo(p.player_id, livePayload(match, p.player_id));
  }

  /**
   * DOES THE GAME AGREE WITH THE LOBBY ABOUT WHO IS ON WHICH SIDE?
   *
   * A pure read. `match.teams` is the lobby's split - the same object the hub was sent - and
   * `match.ingame` is what the gamemode reported, one player per sweep tick.
   *
   * THE TWO SETS OF IDS ARE NOT THE SAME IDS, AND THAT IS THE WHOLE PROBLEM. Ours are 1 and 2 and
   * they carry a meaning (which half of the matchmaker's split). The game's are whatever it chose -
   * measured to be 0 and 1, but we never call SetTeamId, so which of them is "our" team 1 is not
   * knowable from the number. THE HOST PINS THE MAPPING: the host is on our roster, so whatever
   * in-game id the host carries IS our side for the host, and the other in-game id is the other
   * side. Exactly the trick gameReportedScore already uses to attribute a scoreboard.
   *
   * Refusing beats guessing at every step. Teams not decided, the host on no team, the game using
   * one team id or three - each returns a reason rather than an answer, because a gate that guesses
   * would let precisely the divergence it exists to catch through.
   */
  function teamsAgree(match) {
    if (!match) return { ok: false, agree: false, reason: 'no match' };
    const teams = match.teams || (match.lobby && match.lobby.teams) || null;
    if (!teams) return { ok: false, agree: false, reason: 'the teams are not decided' };
    const sideOf = (id) => ((teams[1] || []).includes(id) ? 1 : ((teams[2] || []).includes(id) ? 2 : 0));

    const seen = match.ingame instanceof Map ? match.ingame : new Map();
    const missing = match.players.filter((p) => !seen.has(p.player_id)).map((p) => p.player_id);
    // A player the sweep has not reached yet, or whose TeamID was still -1, is NOT a mismatch. The
    // game leaves TeamID at -1 for the first ~30 s of the match world (measured 2026-09-15), and
    // reading that as a team would fail every match on its first report.
    if (missing.length) {
      return { ok: true, agree: false, reason: 'not every player has reported a team',
               missing, reported: seen.size, total: match.players.length };
    }

    const host = String(match.host || '');
    const hostSide = sideOf(host);
    // The verified writer uses side 1 -> 0, side 2 -> 1. A reversed pre-write snapshot
    // cannot establish a different mapping while the writer is still correcting it.
    if (match.team_sort_verified) {
      const wrong = match.players.filter(p => !sideOf(p.player_id) ||
        seen.get(p.player_id) !== sideOf(p.player_id) - 1)
        .map(p => ({ player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id), want: sideOf(p.player_id),
          got: seen.get(p.player_id) + 1, ingame: seen.get(p.player_id) }));
      return { ok: true, agree: wrong.length === 0, wrong,
        reason: wrong.length ? 'the game does not match the lobby' : '',
        mapping: [{ ingame: 0, side: 1 }, { ingame: 1, side: 2 }] };
    }
    if (!hostSide) return { ok: false, agree: false, reason: 'the host is on no team' };
    const hostTeam = seen.get(host);

    // HOW MANY IN-GAME IDS WE SHOULD EXPECT IS NOT ALWAYS TWO. It is however many of OUR sides
    // actually have players on them. Normally that is two; it is one when a side is empty, which
    // happens in a COMP_MATCH_SIZE=1 test match and also in a real match whose entire second team
    // walked out. Hard-coding 2 makes both of those permanently un-startable, and the second one is
    // not hypothetical - it is the case where the gate would be holding a match that can never
    // satisfy it, which is precisely the failure the bounded deadline exists to survive rather than
    // one it should be asked to absorb.
    const sidesInPlay = [1, 2].filter((side) => match.players.some((pl) => sideOf(pl.player_id) === side));
    const ingameIds = [...new Set(match.players.map(p => seen.get(p.player_id)))].sort((a, b) => a - b);
    if (ingameIds.length !== sidesInPlay.length) {
      return { ok: true, agree: false,
               reason: `the game has everyone on ${ingameIds.length} team id(s), the lobby uses ${sidesInPlay.length}`,
               ingame: ingameIds };
    }
    // One side in play: the host IS that side, everyone reported the same id, and there is nothing
    // left to check. Falling through would try to find an "other" id that does not exist and map
    // every player to undefined.
    if (sidesInPlay.length === 1) {
      return { ok: true, agree: true, host_ingame_team: hostTeam, host_side: hostSide,
               mapping: [{ ingame: hostTeam, side: hostSide }], one_sided: true };
    }
    const otherTeam = ingameIds.find((v) => v !== hostTeam);
    const mapping = new Map([[hostTeam, hostSide], [otherTeam, hostSide === 1 ? 2 : 1]]);

    const wrong = [];
    for (const p of match.players) {
      const want = sideOf(p.player_id);
      if (!want) { wrong.push({ player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id), want: 0, got: 0, why: 'on the roster but on no team' }); continue; }
      const got = mapping.get(seen.get(p.player_id));
      if (got !== want) wrong.push({ player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id), want, got, ingame: seen.get(p.player_id) });
    }
    if (wrong.length) {
      return { ok: true, agree: false, reason: 'the game does not match the lobby', wrong,
               host_ingame_team: hostTeam, host_side: hostSide };
    }
    return { ok: true, agree: true, host_ingame_team: hostTeam, host_side: hostSide,
             mapping: [...mapping].map(([ingame, side]) => ({ ingame, side })) };
  }

  /** Incremental observations are diagnostic only. The full host snapshot is the sole start authority. */
  function goLiveIfReady(match) {
    if (!match || match.state !== 'connecting') return { started: false, reason: 'not connecting' };
    const verdict = teamsAgree(match);
    for (const p of match.players) {
      sendTo(p.player_id, { type: 'match_teams_wait', match_id: match.id,
        reason: verdict.reason || 'Waiting for the game to confirm the complete roster.',
        reported: (match.ingame || new Map()).size, total: match.players.length, deadline: match.deadline || 0 });
    }
    return { started: false, gated: true, verdict };
  }

  // A persisted legacy timer may survive deployment. It can cancel, never authorize a start.
  function expireTeamsGate(matchId) {
    const match = matches.get(matchId);
    if (match?.state === 'connecting') closeMatch(match, 'stalled', []);
  }

  function startReady(hostId, matchId, rows) {
    const host = String(hostId || ''), id = String(matchId || '');
    const reject = error => ({ ok: false, error });
    const match = matches.get(id);
    if (!identity.validPlayer(host) || !match || inMatch.get(host) !== id) return reject('no match');
    if (String(match.host || '') !== host) return reject('not the host');
    if(match.recovery?.phase==='restoring')return reject('recovery is being verified');
    if (!['connecting', 'live'].includes(match.state)) return reject('match is not starting');
    const t = match.assigned_teams;
    if (!Array.isArray(t?.[1]) || !Array.isArray(t?.[2])) return reject('no frozen assignment');
    const ids = [...t[1], ...t[2]].map(String), unique = new Set(ids);
    if (duel && (ids.length !== 2 || t[1].length !== 1 || t[2].length !== 1)) return reject('invalid duel assignment');
    if (ids.length < (soloMatch(match)?1:2) || ids.length > 10 || unique.size !== ids.length || ids.some(x => !identity.validPlayer(x)))
      return reject('invalid frozen assignment');
    const sideOf = new Map([...t[1].map(x => [String(x), 0]), ...t[2].map(x => [String(x), 1])]);
    if (!Array.isArray(rows) || rows.length !== ids.length) return reject('wrong roster size');
    const reported = new Map();
    for (const row of rows) {
      if (!row || !unique.has(row.player_id) || reported.has(row.player_id)) return reject('wrong roster');
      if (row.active !== 1) return reject('inactive player');
      if (row.team !== sideOf.get(row.player_id)) return reject('wrong team');
      reported.set(row.player_id, row.team);
    }
    if (match.state === 'live') return { ok: true, already: true, match_id: id };
    const current = new Map(match.players.map(p => [String(p.player_id), p]));
    if (current.size !== ids.length || ids.some(x => !current.has(x) || inMatch.get(x) !== id))
      return reject('assigned roster changed');
    // The host's actual PlayerStates/controllers are stronger evidence than a hub launch claim.
    for (const p of match.players) p.connected = true;
    match.ingame = reported;
    match.team_sort_verified = true;
    match.start_ready_verified = true;
    goLive(match, { ok: true, agree: true, verified: true,
      mapping: [{ ingame: 0, side: 1 }, { ingame: 1, side: 2 }] });
    return { ok: true, started: true, match_id: id };
  }

  async function reconnectPenalty(match, id, {recovery=false}={}) {
    const recoveryMiss=recovery&&match.recovery?.roster?.excluded.includes(id)&&
      Date.now()>=match.recovery.rejoin_until;
    if (noShowPenaltiesPaused()&&!recoveryMiss) return null;
    return withRatingLocks([id], async () => {
      for (let attempt=0; attempt<4; attempt++) {
        await drainRatingWrites(id); await drainPenaltyWrites(id);
        const rawRank=store?await store(['GET',ratingKey(id)],{strict:true}):null;
        const rawPenalty=store?await store(['GET',penaltyKey(id)],{strict:true}):null;
        const before=store?ratingLib.normalise(rawRank?identity.parse(rawRank):{}):ratingOf(id);
        const prior=store?(rawPenalty?identity.parse(rawPenalty):{}):penalties.get(id)||{};
        const count=effectiveCount(prior)+1, seconds=rungSeconds(count), now=Date.now();
        const hit=progressLib.penalise(before,NO_SHOW_RR);
        const rank={...before,progress:hit.progress,demoteArmed:hit.delta?false:before.demoteArmed,revision:(before.revision||0)+1};
        const penalty={until:now+seconds*1000,last:now,reason:'reconnect_timeout',count,elo:(prior.elo||0)+Math.abs(hit.delta)};
        const receipt={...penalty,seconds,rr:Math.abs(hit.delta),elo:Math.abs(hit.delta),match_id:match.id,player_id:id,rank,
          next_seconds:rungSeconds(count+1)};
        const penaltyJson=JSON.stringify(penalty);
        try {
          const saved=store?await require('./abandon-penalty.cjs').commit(store,prefix||'hub:',match.id,id,{
            host:match.host,host_epoch:match.host_epoch||0,roster_revision:match.roster_revision||0,
            expectedRank:before.revision||0,expectedPenalty:rawPenalty||'',rankJson:JSON.stringify(rank),penaltyJson,
            guardJson:JSON.stringify({operationId:`reconnect:${match.id}`,json:penaltyJson}),
            receiptJson:JSON.stringify(receipt),placing:ratingLib.isPlacing(rank),progress:boardScore(rank),
            ttl:Math.max(LIVE_STATE_TTL_SECONDS,seconds+14*86400)
          }):{receipt,replayed:false};
          // A replay may be older than another result: reload the current values.
          const currentRank=saved.replayed&&store?identity.parse(await store(['GET',ratingKey(id)],{strict:true})):saved.receipt.rank;
          const currentPenalty=saved.replayed&&store?identity.parse(await store(['GET',penaltyKey(id)],{strict:true})):penalty;
          ratings.set(id,currentRank); ratingLoaded.add(id); noteRating(id,currentRank);
          penalties.set(id,currentPenalty); penaltyLoaded.add(id);
          const {rank:_rank,...visible}=saved.receipt;
          if(!saved.replayed)sendTo(id,{type:'penalty',...visible});
          return visible;
        } catch(e) { if(!e.conflict||attempt===3)throw e; }
      }
    });
  }

  function matchPresence(host, id, present) {
    return matchOperation(id, async () => {
      const match=matches.get(id);
      if(match)await refreshAuthority(match);
      if(!match || match.terminal || match.void_pending || match.host!==host || inMatch.get(host)!==id)return {ok:false,error:'not the host'};
      if(match.recovery?.phase==='restoring')return {ok:false,error:'recovery is being verified'};
      const observation=require('./reconnect.cjs').observe(match,present);
      if(!observation.ok)return observation;
      const previous=match.reconnect;
      const changed=JSON.stringify(previous||{})!==JSON.stringify(observation.windows);
      try {
        match.reconnect=observation.windows;
        // Save the deadline before a penalty can be applied; reloads cannot grant another five minutes.
        if(changed) {
          const membershipFrom=match.roster_revision||0;
          match.roster_revision=membershipFrom+1;
          try { await persistLive(match,{membershipFrom}); }
          catch(e) { match.reconnect=previous;match.roster_revision=membershipFrom;throw e; }
          if(match.roster_revision!==membershipFrom+1)return {ok:false,error:'match decision changed'};
        }
        if(matches.get(id)!==match || match.state!=='live' || match.final_snapshot)return {ok:false,error:'match ended'};
        if(duel&&observation.expired.length===1){
          const player=observation.expired[0];
          return decideDuel(match,player,'reconnect_timeout');
        }
        const removed=[],departures=[];
        const beforeRemoval={players:match.players,left:match.left,reconnect:{...match.reconnect},terminal:match.terminal,roster_revision:match.roster_revision};
        for(const player of observation.expired) {
          const member=match.players.find(p=>p.player_id===player);
          if(!member)continue;
          const penalty=await reconnectPenalty(match,player);
          const lastStats=match.stats?.players?.find(p=>p.steamId===player);
          departures.push({...member,at:Date.now(),left_state:'live',disconnect_confirmed:true,
            reason:'reconnect_timeout',penalty,last_stats:lastStats?structuredClone(lastStats):null});
          removed.push(player);
        }
        if(removed.length) {
          const membershipFrom=match.roster_revision||0;
          match.roster_revision=membershipFrom+1;
          match.left=[...(match.left||[]),...departures];
          match.players=match.players.filter(p=>!removed.includes(p.player_id));
          match.reconnect={...match.reconnect};
          for(const player of removed)delete match.reconnect[player];
          const remainingSides=[1,2].filter(side=>match.players.some(p=>match.assigned_teams?.[side]?.includes(p.player_id)));
          if(remainingSides.length===1){
            const winner=remainingSides[0],loser=match.left.find(p=>match.assigned_teams?.[winner===1?2:1]?.includes(p.player_id))?.player_id;
            match.terminal={reason:'reconnect_timeout',absence:true,loser,winner,at:Date.now(),score:match.score?{...match.score}:null};
          }
          try { await persistLive(match,{membershipFrom}); }
          catch(e) { Object.assign(match,beforeRemoval); throw e; }
          if(matches.get(id)!==match || match.state!=='live' || match.final_snapshot)return {ok:false,error:'match ended'};
          if(match.roster_revision!==membershipFrom+1)return {ok:false,error:'match decision changed'};
          for(const player of removed) {
            inMatch.delete(player); revokeJoinPermit(player);
            sendTo(player,{type:'match_over',match_id:id,reason:'reconnect_timeout'});
          }
          if(!match.terminal)for(const member of match.players)sendTo(member.player_id,livePayload(match,member.player_id));
        }
        if(match.terminal?.absence){
          await finishDuelDecision(match);
          return {ok:false,error:'Match decided by forfeit'};
        }
        const waiting=Object.entries(match.reconnect).map(([player_id,w])=>({player_id,deadline:w.deadline}));
        for(const p of match.players)sendTo(p.player_id,{type:'match_reconnect',match_id:id,waiting});
        return {ok:waiting.length===0,match_id:id,waiting,error:waiting.length?'waiting for reconnect':''};
      } catch { return {ok:false,error:'reconnect state is not saved'}; }
    });
  }

  /**
   * THE GAME REPORTING ONE PLAYER'S IN-GAME TEAM. GM_BB5's sweep visits one player per tick and
   * sends ch_team_verified after the write; this is where the row lands.
   *
   * Same trust window as gameReportedIn and teamRuling, for the same reason: /api/probe is
   * unauthenticated, so the host must already be the host of a match already in `connecting` or
   * `live`, and the subject must already be on that match's roster. The worst an outsider can do
   * with it is delay a start they already knew about, which the bounded gate then ends anyway.
   *
   * A NEGATIVE TEAM ID IS "NOT YET", NOT A TEAM. ABodycamPlayerState::TeamID is -1 until the game
   * assigns one (measured: the first ~30 s of the match world), and GetPlayerScore does the same
   * trick with -1. Storing it would make every early sweep look like a mismatch, so it FORGETS any
   * value we had for that player instead - the gate then waits for a real one rather than starting
   * on stale agreement.
   */
  function gameReportedTeam(hostId, report) {
    const host = String(hostId || '');
    const subject = String((report && report.subject) || '');
    if (!identity.validPlayer(host)) return { ok: false, error: 'not a steamid64' };
    if (!identity.validPlayer(subject)) return { ok: false, error: 'subject is not a steamid64' };
    const team = Number.parseInt(String((report && report.team) !== undefined ? report.team : ''), 10);
    if (!Number.isFinite(team)) return { ok: false, error: 'no team id' };

    const matchId = inMatch.get(host);
    const match = matchId && matches.get(matchId);
    if (!match) return { ok: false, error: 'no match' };
    if (match.state !== 'connecting' && match.state !== 'live') {
      return { ok: false, error: 'match is not connecting or live' };
    }
    if (String(match.host || '') !== host) return { ok: false, error: 'not the host' };
    if (!match.players.some((p) => p.player_id === subject)) return { ok: false, error: 'not on the roster' };

    if (match.state === 'live' && match.start_ready_verified) return { ok: true, ignored: true };

    if (report.verified && !match.team_sort_verified) {
      match.team_sort_verified = true;
      match.ingame = new Map();
    }
    if (!(match.ingame instanceof Map)) match.ingame = new Map();
    if (team < 0) {
      match.ingame.delete(subject);
      return { ok: true, match_id: match.id, subject, team, ready: false,
               reported: match.ingame.size, total: match.players.length };
    }
    match.ingame.set(subject, team);

    const verdict = teamsAgree(match);
    const started = goLiveIfReady(match);
    return { ok: true, match_id: match.id, subject, team, ready: true,
             reported: match.ingame.size, total: match.players.length,
             agree: Boolean(verdict.agree), reason: verdict.reason || '',
             started: Boolean(started && started.started) };
  }

  // ---------------------------------------------------------------- remembered profiles
  //
  // WHY THIS EXISTS (Sam, 2026-09-16). The leaderboard and the friends list rendered a bare
  // 17-digit SteamID for every player who was not connected at that instant, which is most of a
  // leaderboard most of the time. Both read `personaOf`, and everything `personaOf` knew came from
  // a LIVE CLIENT or a match in progress - so the moment somebody closed their hub they turned
  // back into a number, on everyone else's screen, including their own friends'.
  //
  // A name is not live state and should not have been kept like it. This remembers one per player,
  // written through whenever we learn it from Steam and read back lazily, exactly the way ratings
  // and penalties already work in this file.
  //
  // WHERE A NAME COMES FROM, in order of how much we trust it:
  //   1. a live client of theirs        - what `whoami` had when their stream came up, minutes old
  //   2. a match they are standing in   - the roster carries the personas it formed with
  //   3. this store                     - the last one we ever saw, however long ago
  //   4. Steam itself                   - `profileOf`, auth's day-cached GetPlayerSummaries, for
  //                                       a player this process has never met. Bounded, see below.
  const PROFILE_BACKFILL = Math.max(0, Number(process.env.COMP_PROFILE_BACKFILL) || 24);
  const profiles = new Map();          // steamId -> { persona, avatar, updated }
  const profileLoaded = new Set();     // the store has been read for this id (hit or miss)
  const profileAsked = new Set();      // Steam has been asked once, this process, for this id

  /** Write a name through, from any source that actually knows one.
   *
   * A BLANK NEVER OVERWRITES A NAME. `whoami` answers with persona '' when STEAM_WEB_API_KEY is
   * unset or Steam is having a bad minute (auth.fetchProfile returns null and is deliberately not
   * cached), and one such sign-in would otherwise erase a name we had been holding for weeks. The
   * worst a stale name can do is be old; the worst a blank can do is be a 17-digit number. */
  function rememberProfile(steamId, persona, avatar) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return null;
    const known = profiles.get(id) || { persona: '', avatar: '', updated: 0 };
    const next = { persona: String(persona || '').trim() || known.persona,
                   avatar: String(avatar || '').trim() || known.avatar,
                   updated: Date.now() };
    profiles.set(id, next);
    profileLoaded.add(id);
    // Nothing to persist until we have a NAME: an avatar on its own still renders as a number, and
    // a key holding only a picture would count as "loaded" and stop the backfill ever running.
    if (!store || !next.persona) return next;
    if (next.persona === known.persona && next.avatar === known.avatar) return next;
    // No TTL, for the reason saveRating gives: a player who takes three months off should come
    // back as themselves. A name that changed is corrected on their next sign-in, above.
    Promise.resolve(store(['SET', profileKey(id), JSON.stringify(next)]))
      .catch(() => { /* the in-memory copy still serves this process */ });
    return next;
  }

  async function loadProfile(steamId) {
    const id = String(steamId || '');
    if (profileLoaded.has(id) || !store || !identity.validPlayer(id)) return profiles.get(id) || null;
    profileLoaded.add(id);
    try {
      const raw = await store(['GET', profileKey(id)]);
      const parsed = raw && (typeof raw === 'string' ? identity.parse(raw) : identity.hydrate(structuredClone(raw)));
      const known = profiles.get(id);
      // Anything this process learned while the read was in flight came from a live client and is
      // newer by definition - the same rule loadRating uses.
      if (parsed && parsed.persona && !known) {
        profiles.set(id, { persona: String(parsed.persona || ''),
                           avatar: String(parsed.avatar || ''),
                           updated: Number(parsed.updated) || 0 });
      }
    } catch { /* a missing or malformed record is simply a player we have never met */ }
    return profiles.get(id) || null;
  }

  /** Have names ready for a list of ids before something synchronous goes looking for them.
   *
   * `personaOf` is called from row builders that cannot await, so the awaiting is done here, once,
   * by the async caller that already awaits its ratings or its friend set.
   *
   * THE BACKFILL IS BOUNDED THREE WAYS, because it is the one part of this that leaves the
   * process: only ids missing a name or avatar, at most PROFILE_BACKFILL of them per call, and once per
   * id per process (`profileAsked`) however many times the page is refreshed. Behind it sits
   * auth's own day-long cache, so a full leaderboard costs at most fifty Steam lookups on the
   * first load after a redeploy and none thereafter. With no STEAM_WEB_API_KEY, `profileOf`
   * answers blank and this is a no-op - the board is no worse than it is today. */
  async function loadProfiles(steamIds) {
    const ids = [...new Set((steamIds || []).map((x) => String(x || '')))]
      .filter((id) => identity.validPlayer(id));
    if (!ids.length) return;
    await Promise.all(ids.map((id) => loadProfile(id).catch(() => null)));
    if (typeof profileOf !== 'function' || !PROFILE_BACKFILL) return;
    const missing = ids
      .filter((id) => identity.validSteam(id) && !profileAsked.has(id) && (!personaOf(id) || !avatarOf(id)))
      .slice(0, PROFILE_BACKFILL);
    await Promise.all(missing.map(async (id) => {
      profileAsked.add(id);
      try {
        const got = await profileOf(id);
        if (got && got.persona) rememberProfile(id, got.persona, got.avatar);
      } catch { /* Steam is optional; a nameless row is what we had before */ }
    }));
  }

  /** The best name we know for a steam id: a live client, then any match they are in, then the
   *  last one we were ever told. See the block above for why step three exists. */
  function personaOf(steamId) {
    for (const clientId of bySteam.get(steamId) || []) {
      const c = clients.get(clientId);
      if (c && c.persona) return c.persona;
    }
    const matchId = inMatch.get(steamId);
    const match = matchId && matches.get(matchId);
    const p = match && everyone(match).find((x) => x.player_id === steamId);
    if (p && p.persona) return p.persona;
    return (profiles.get(String(steamId || '')) || {}).persona || '';
  }

  /** The best avatar we know for a steam id, looked up exactly like the persona above. */
  function avatarOf(steamId) {
    for (const clientId of bySteam.get(steamId) || []) {
      const c = clients.get(clientId);
      if (c && c.avatar) return c.avatar;
    }
    const matchId = inMatch.get(steamId);
    const match = matchId && matches.get(matchId);
    const p = match && everyone(match).find((x) => x.player_id === steamId);
    if (p && p.avatar) return p.avatar;
    return (profiles.get(String(steamId || '')) || {}).avatar || '';
  }

  // ---------------------------------------------------------------- account bans
  //
  // Sam, 2026-09-15: "so if a user gets banned they can no longer use that steam account to sign
  // into competitive".
  //
  // TIED TO THE STEAM ID, like everything else that has to survive a reinstall: reports, ratings,
  // friends. The hub has no account of its own to ban (auth.cjs: no email, no password), and that
  // is a feature here - the identity being banned is the one Steam proves, so a new hub install,
  // a new machine or a cleared state.json changes nothing.
  //
  // It is NOT the no-show ladder. That is a cooldown measured in minutes that expires on its own
  // and is applied by the server automatically; this is a human decision with a name attached.
  // Keeping them separate means a stack of no-shows can never quietly become a ban, and a ban is
  // never mistaken for one.
  async function loadBan(steamId) {
    const id = String(steamId || '');
    if (!store) return bans.get(id) || null;
    // Every authenticated request observes durable moderation from other instances.
    // An unavailable check is retryable; it must not cache permanent permission.
    const raw = await store(['GET', banKey(id)], {strict:true,timeout:5000});
    if (raw) bans.set(id, typeof raw === 'string' ? identity.parse(raw) : identity.hydrate(structuredClone(raw)));
    else bans.delete(id);
    bansLoaded.add(id);
    return bans.get(id) || null;
  }

  /** The live ban on an account, or null. Expired bans are treated as gone. */
  function banOf(steamId) {
    const rec = bans.get(String(steamId || ''));
    if (!rec) return null;
    if (rec.until && rec.until <= Date.now()) return null;
    return rec;
  }

  async function banAccount(adminId, body) {
    if (!isAdmin(adminId)) return { ok: false, error: 'not an admin' };
    const target = String((body && (body.player_id || body.steam_id)) || '');
    if (!identity.validPlayer(target)) return { ok: false, error: 'not a steamid64' };
    if (isAdmin(target)) return { ok: false, error: 'that account is an admin' };
    const match = matches.get(inMatch.get(target)) || [...matches.values()].find(m =>
      m.players.some(p => identity.gameFor(m, p.player_id) === target));
    const owner = match?.players.find(p => p.player_id === target || identity.gameFor(match, p.player_id) === target)?.player_id || target;
    if (isAdmin(owner)) return { ok: false, error: 'that account is an admin' };
    return matchOperation(match?.id || 'ban:'+target, async () => {
    // Serialise against local final reports, and atomically save the durable void
    // with the ban so a restart cannot leave a banned player's match running.
    const active = match && matches.get(match.id) === match && match.state === 'live';
    if (active && !match.collecting && !match.settling && !match.final_snapshot) await persistLive(match);
    const matchKeys = active ? [liveMatchKey(match.id), settlementKey(match.id)] : [];
    if(body?.category==='cheating'){
      const saved=await restitution.begin(String(adminId),{target,operation_id:body.operation_id,reason:body.reason},matchKeys);
      bans.set(target,saved.ban);bansLoaded.add(target);
      await voidMatchForBan(match);
      try{removeFromQueue(owner);}catch{}
      for(const clientId of [...(bySteam.get(owner)||[])]){if(competitionGuard)send(clientId,{type:'ranked_ban',ban:saved.ban});else{send(clientId,{type:'banned',reason:'Cheating',until:0});drop(clientId);}}
      void restitution.run().catch(()=>{});
      return {ok:true,banned:target,until:0,corrections:'queued',decision_id:saved.decision.id};
    }
    const days = Number((body && body.days) || 0);
    const rec = {
      at: Date.now(),
      by: String(adminId),
      reason: String((body && body.reason) || '').slice(0, 200),
      // 0 or absent means permanent. Stored as an absolute instant rather than a duration so it
      // cannot quietly restart itself on a redeploy.
      until: days > 0 ? Date.now() + days * 24 * 3600 * 1000 : 0,
    };
    if (store) await restitution.setBan(String(adminId),target,rec,matchKeys);
    bans.set(target, rec);
    bansLoaded.add(target);
    await voidMatchForBan(match);
    // Put them out of whatever they are in right now, or the ban does not start until they
    // happen to close the app.
    try { removeFromQueue(owner); } catch { /* not queued */ }
    for (const clientId of [...(bySteam.get(owner) || [])]) {
      if(competitionGuard)send(clientId,{type:'ranked_ban',ban:rec});
      else{send(clientId, { type: 'banned', reason: rec.reason, until: rec.until });drop(clientId);}
    }
    console.log('[admin] %s banned %s%s (%s)', adminId, target,
                rec.until ? ` until ${new Date(rec.until).toISOString()}` : ' permanently',
                rec.reason || 'no reason given');
    return { ok: true, banned: target, until: rec.until };
    });
  }

  async function unbanAccount(adminId, body) {
    if (!isAdmin(adminId)) return { ok: false, error: 'not an admin' };
    const target = String((body && (body.player_id || body.steam_id)) || '');
    if (!identity.validPlayer(target)) return { ok: false, error: 'not a steamid64' };
    if (store) await restitution.setBan(String(adminId),target,null);
    bans.delete(target);
    bansLoaded.add(target);
    if(competitionGuard)sendTo(target,{type:'ranked_ban',ban:null});
    console.log('[admin] %s unbanned %s', adminId, target);
    return { ok: true, unbanned: target };
  }

  // ---------------------------------------------------------------- admin console
  function isAdmin(steamId) { return ADMIN_IDS.has(String(steamId || '')); }

  /**
   * Everything the console shows, in one read. Deliberately ONE call: a console that fires six
   * requests to draw a page is six things that can half-fail and leave a moderator looking at a
   * screen that is partly yesterday.
   *
   * IT IS READ-ONLY. There is no ban button, no rating edit, no report deletion - not because
   * those are hard, but because the first version of a moderation tool should not be able to do
   * anything irreversible while nobody has yet agreed what the rules are. It answers "who should
   * I look at", which is the question that actually exists today.
   */
  async function adminOverview(steamId, opts) {
    if (!isAdmin(steamId)) return { ok: false, error: 'not an admin' };
    const limit = Math.max(1, Math.min(200, Number((opts && opts.limit)) || 50));
    await loadReportIndex();

    // NAMES FIRST, for the same reason friendList and leaderboard do it: a moderator is reading
    // ABOUT people, not with them, so most of both lists below are accounts no live client can
    // name. This is the screen the duplicate-personaOf bug was originally REPORTED against, and
    // it was the one caller the fix did not reach.
    await loadProfiles([...reports.keys(), ...bans.keys()]);

    // Reported players, worst first, including records restored after a redeploy.
    const reported = [...reports.entries()]
      .filter(([, list]) => list.length > 0)
      .map(([id, list]) => ({
        player_id: id,
        persona: personaOf(id),
        total: list.length,
        reporters: new Set(list.map((r) => r.by)).size,
        last: list.length ? list[0].at : 0,
        by_reason: list.reduce((acc, r) => { acc[r.reason] = (acc[r.reason] || 0) + 1; return acc; }, {}),
        reports: list.map((r) => ({ at: r.at, by: r.by, reason: r.reason, note: r.note || '' })),
      }))
      // Distinct reporters first: ten reports from one person is one person with a grudge, three
      // from three different people is a pattern. Sorting by raw count would put the grudge top.
      .sort((a, b) => (b.reporters - a.reporters) || (b.total - a.total) || (b.last - a.last))
      .slice(0, limit);

    // Team kills, from the matches this process has seen. Same honesty: live and archived only.
    const kills = [];
    for (const m of [...matches.values(), ...archived.values()]) {
      for (const k of (m.teamkills || [])) {
        if (k.flags && k.flags.length) kills.push({ ...k, match_id: m.id });
      }
    }
    kills.sort((a, b) => b.at - a.at);

    // LIVE MATCHES, in enough detail to watch one. Sam: "see the stats of live games currently
    // going on ... we should be able to see live data as rounds go on". The round timeline is
    // already kept for the match-detail screen, so watching a match in progress is the same data
    // read while it is still moving.
    const live = [...matches.values()].map((m) => ({
      id: m.id,
      state: m.state,
      map: m.map || '',
      host: m.host || '',
      started: m.created || 0,
      score: m.score || null,
      score_limit: m.score_limit || null,
      // Round by round, so the console shows HOW it is going rather than only where it stands.
      rounds: (m.rounds || []).map((r) => ({ at: r.at, won: r.won, 1: r[1], 2: r[2] })),
      teams: m.teams || {},
      players: (m.players || []).map((p) => ({
        player_id: p.player_id, game_steam_id: identity.gameFor(m, p.player_id), persona: p.persona || '',
        team: teamOf(m, p.player_id), connected: Boolean(p.connected), left: Boolean(p.left),
      })),
      // The two things a moderator watching a live match actually wants flagged.
      team_kills: (m.teamkills || []).filter((k) => k.flags && k.flags.length).length,
      teams_agreed: Boolean(m.teams_verdict && m.teams_verdict.agree),
    }));

    return {
      ok: true,
      now: Date.now(),
      // Bug reports ride with the overview the console already reads rather than sitting behind a
      // second request: a screen that refreshes itself every 20 seconds should do it in one.
      bugs: await bugReports(limit),
      reported,
      team_kills: kills.slice(0, limit),
      matches: live,
      queue: { units: queue.length, players: queuedPlayers() },
      banned: [...bans.entries()]
        .filter(([, rec]) => !rec.until || rec.until > Date.now())
        .map(([id, rec]) => ({ player_id: id, persona: personaOf(id), at: rec.at, by: rec.by,
                               reason: rec.reason || '', until: rec.until || 0 }))
        .sort((a2, b2) => b2.at - a2.at),
      online: bySteam.size,
      accounts: await adminAccountSummary(steamId),
      // What the moderator needs to know to read the rest of the page honestly.
      notes: {
        enforcement: tkEnforcing() ? 'on' : 'off',
        memory_only: true,
        admins: ADMIN_IDS.size,
      },
    };
  }

  // ---------------------------------------------------------------- the player directory
  //
  // Sam, 2026-09-16: "a player search either through steamid or steam name ... each player thats
  // ever logged on being a row and the columns have various info".
  //
  // WHAT IT IS NOT. It is not a second copy of the ladder. Wins, losses, MMR and RR belong to the
  // rating record and are only ever WRITTEN there; what this keeps is a mirror of them (noteRating,
  // below) so a thousand-row table does not become a thousand reads, plus the facts nothing else
  // writes down at all: when an account first signed in, how many hours it has played, what the
  // kill feed said it did.
  //
  // WHY A HASH AND NOT A KEY PER PLAYER. Everything else here is keyed per player - rating:<id>,
  // persona:<id>, ban:<id> - which is right for the hub, where every read is about one person, and
  // exactly wrong for a directory, where every read is about all of them. One field per player in
  // ONE hash means the whole table arrives in a single HGETALL, and a search by name can be
  // answered without a read per row.

  /** A player nobody has written anything down about yet. Every field is present and typed, so a
   *  row never has to work out what a missing one meant. */
  function blankCareer(steamId) {
    return {
      player_id: String(steamId || ''),
      persona: '',
      first_seen: 0, last_seen: 0, sessions: 0,
      hub: '', mode: '',
      // The mirror of the rating record. Written by noteRating and read by nothing else.
      rated: 0, wins: 0, losses: 0, mmr: 0, progress: 0,
      // Ours alone.
      played: 0, abandons: 0, no_shows: 0,
      kills: 0, deaths: 0, team_kills: 0, clutches: 0, rounds: 0, seconds: 0,
      reports_in: 0, reporters: 0, reports_out: 0,
      last_match: 0, last_map: '',
    };
  }

  function careerOf(steamId) {
    const id = String(steamId || '');
    return careers.get(id) || blankCareer(id);
  }

  /** Write a directory record through. Fire and forget, like every other write on this service:
   *  a directory that could not be saved must never cost anybody their match. */
  function saveCareer(steamId, record) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return record;
    if (frozenCareers.has(id)) return careerOf(id);
    const clean = { ...record, player_id: id };
    careers.set(id, clean);
    if (store) {
      trackWrite(careerWrites, id, store(['EVAL', require('./result-commit.cjs').CAREER_SCRIPT,
        '1', rosterKey(), id, JSON.stringify(clean)]));
    }
    return clean;
  }

  /**
   * SOMEBODY SIGNED IN. The write that makes "every player that has ever logged on" true.
   *
   * Called from handleStream - the single door every signed-in hub comes through, and the same
   * reason rememberProfile is called there. A player who signs in and never queues still gets a
   * row, which is the point: this is a directory of ACCOUNTS, not of competitors.
   */
  function noteSeen(steamId, persona, account = {}) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return null;
    const rec = { ...careerOf(id) };
    const now = Date.now();
    if (!rec.first_seen) rec.first_seen = now;
    rec.last_seen = now;
    rec.sessions = (rec.sessions || 0) + 1;
    // A blank NEVER erases a name - rememberProfile's rule, and for its reason: a hub running
    // without STEAM_WEB_API_KEY signs in with an empty persona.
    const name = String(persona || '').trim().slice(0, 64);
    if (name) rec.persona = name;
    if (identity.validSteam(account.game_steam_id)) rec.game_steam_id = account.game_steam_id;
    if (['steam','lightsout'].includes(account.auth_method)) rec.auth_method = account.auth_method;
    const v = versions.get(id);
    if (v && v.hub) rec.hub = String(v.hub).slice(0, 32);
    if (v && v.mode) rec.mode = String(v.mode).slice(0, 32);
    return saveCareer(id, rec);
  }

  /**
   * The ladder moved, so the mirror moves with it.
   *
   * Called from saveRating and loadRating, which are the only two places a rating enters this
   * process. Nothing reads these fields but the directory, and nothing writes them but this - a
   * second counter for a fact somebody else owns is how two numbers come to disagree.
   */
  function noteRating(steamId, record) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return;
    const rec = careerOf(id);
    const next = {
      ...rec,
      rated: record.matches || 0,
      wins: record.wins || 0,
      losses: record.losses || 0,
      mmr: Math.round(record.rating || 0),
      progress: record.progress || 0,
    };
    // Nothing changed: the common case on a stream coming up, and not worth a write.
    if (next.rated === rec.rated && next.wins === rec.wins && next.losses === rec.losses
        && next.mmr === rec.mmr && next.progress === rec.progress && careers.has(id)) return;
    saveCareer(id, next);
  }

  /** A report was filed. Both ends of it are counted: what was said about them, and what they
   *  said about other people - a moderator reading an accusation wants to know both. */
  function noteReport(by, target, list) {
    const rows = list || [];
    const them = { ...careerOf(target) };
    them.reports_in = rows.length;
    them.reporters = new Set(rows.map((r) => r.by)).size;
    saveCareer(target, them);
    const me = { ...careerOf(by) };
    me.reports_out = (me.reports_out || 0) + 1;
    saveCareer(by, me);
  }

  /**
   * WHAT A MATCH ADDED TO EVERYONE WHO WAS IN IT. Called once, as the match ends.
   *
   * Wins, losses and rank are NOT counted here. The rating record already counts them and the
   * mirror above already carries them; counting them a second time is how the console comes to
   * disagree with the player's own profile. What is counted is what nothing else writes down.
   *
   * KILLS COME FROM THE FEED WHEN THERE IS ONE. `match.kills` is one named event per kill, so a
   * kill is a kill and a team kill is not one of them. The stat row's `k` is a NET score that a
   * team kill DECREMENTS (measured 2026-09-15), so it is only read when no feed arrived, and then
   * floored at zero: a negative net is not "minus two kills", it is a player whose team kills
   * outnumbered their kills, and those have a column of their own.
   */
  function creditMatch(match, opts = {}) {
    try {
      if (!match || match.credited) return;
      identity.hydrate(match);
      if (!opts.dryRun) match.credited = true;
      const credited = {};
      const now = opts.now || Date.now();
      const feed = (match.kills || []).filter((k) => k && (k.killer || k.victim));
      const rows = new Map(((match.stats && match.stats.players) || [])
        .map((r) => [r.steamId, r]));
      const rounds = Math.max(0, Number(match.stats && match.stats.rounds) || 0);
      // Only a match that actually went live has minutes in it. A lobby that nobody connected to
      // took the players' time, but it is not time PLAYED and counting it as such would make the
      // column useless exactly where it matters - on the account that keeps not turning up.
      const seconds = opts.played
        ? Math.max(0, Math.round((now - (match.live_at || match.created || now)) / 1000))
        : 0;
      const blamed = new Set(opts.blame || []);
      const walked = new Set((match.left || []).map((p) => p.player_id));
      for (const p of everyone(match)) {
        const id = p.player_id;
        if (!identity.validPlayer(id)) continue;
        const rec = { ...careerOf(id) };
        const row = rows.get(id);
        if (feed.length) {
          rec.kills += feed.filter((k) => k.killer === id && !k.teamKill && !k.suicide).length;
          rec.deaths += feed.filter((k) => k.victim === id).length;
          rec.team_kills += feed.filter((k) => k.killer === id && k.teamKill).length;
        } else {
          if (row) {
            rec.kills += Math.max(0, Number(row.kills) || 0);
            rec.deaths += Math.max(0, Number(row.deaths) || 0);
          }
          rec.team_kills += (match.teamkills || []).filter((k) => k.killer === id).length;
        }
        if (row && Number.isFinite(Number(row.clutches))) {
          rec.clutches += Math.max(0, Number(row.clutches));
        }
        if (opts.played) {
          rec.played += 1;
          rec.rounds += rounds;
          rec.seconds += seconds;
          rec.last_match = now;
          if (match.map) rec.last_map = String(match.map).slice(0, 64);
        }
        // Walking out is this player's own offence whatever became of the match afterwards -
        // historyRow's rule, and the same two sources: the blame list, and the leavers.
        if (!opts.played && (blamed.has(id) || walked.has(id))) {
          rec.abandons += 1;
          if (opts.reason === 'no_show') rec.no_shows += 1;
        }
        if (p.persona) rec.persona = String(p.persona).slice(0, 64) || rec.persona;
        if (opts.dryRun) credited[id] = rec;
        else saveCareer(id, rec);
      }
      return credited;
    } catch { /* a directory that could not be updated is not worth a match */ }
  }

  /**
   * The whole directory, once per process.
   *
   * Memoised as a PROMISE rather than a flag: the console refreshes itself, so two requests can
   * easily be in flight together, and a flag would let the second one read a half-filled map.
   */
  async function loadRoster() {
    if (!store) return Promise.resolve(careers);
    if (!rosterRead) {
      rosterRead = (async () => {
        try {
          const raw = await store(['HGETALL', rosterKey()]);
          if (!Array.isArray(raw) && (!raw || typeof raw !== 'object')) throw Error('Directory unavailable');
          // Upstash answers a hash as a flat [field, value, field, value, ...]. A plain object is
          // accepted too, so a local stand-in store does not have to imitate the wire.
          const pairs = [];
          if (Array.isArray(raw)) {
            for (let i = 0; i + 1 < raw.length; i += 2) pairs.push([raw[i], raw[i + 1]]);
          } else if (raw && typeof raw === 'object') {
            pairs.push(...Object.entries(raw));
          }
          for (const [field, value] of pairs) {
            const id = String(field);
            if (!identity.validPlayer(id)) continue;
            // A record written by THIS process while the read was in flight is newer and wins -
            // the same rule loadRating and loadPenalty use.
            if (careers.has(id)) continue;
            let rec;
            try { rec = typeof value === 'string' ? identity.parse(value) : value; } catch { continue; }
            if (rec && typeof rec === 'object') {
              careers.set(id, { ...blankCareer(id), ...rec, player_id: id });
            }
          }
          rosterAvailable = true;
        } catch { rosterAvailable = false; }
        return careers;
      })();
    }
    await rosterRead;
    if (!rosterAvailable) rosterRead = null;
    return careers;
  }

  /**
   * EVERYONE WHO WAS HERE BEFORE THIS SHIPPED, adopted from the leaderboard.
   *
   * The directory starts empty on the day it deploys, and a player search that only finds people
   * who have signed in since is a search that looks broken. The board is the one index of players
   * this service already keeps, so it is read once and every id on it is given a row. The row is
   * bare - the ladder mirror fills it the next time their rating is read, the name the next time
   * the page lists them - which is honest: we know they exist and little else yet.
   */
  async function adoptLadder() {
    if (!store || ladderAdopted) return;
    try {
      const ids = await store(['ZRANGE', boardKey(), '0', '-1']);
      if (!Array.isArray(ids)) throw Error('Leaderboard unavailable');
      for (const raw of (Array.isArray(ids) ? ids : [])) {
        const id = String(raw);
        if (identity.validPlayer(id) && !careers.has(id)) careers.set(id, blankCareer(id));
      }
      ladderAdopted = true;
    } catch { /* no board, no adoption; the directory is simply the people we have seen */ }
  }

  /** Where a player is right now, as one word the table can sort on. */
  function statusOf(steamId) {
    if (inMatch.has(steamId)) return 'match';
    if (isQueued(steamId)) return 'queued';
    if (bySteam.has(steamId)) return 'online';
    return 'offline';
  }

  /** One row, built from everything this process knows about that account. */
  function directoryRow(steamId) {
    const id = String(steamId);
    const c = careerOf(id);
    const ban = banOf(id);
    // The rank the PLAYER sees, from the same call the hub and the match result use. Built from
    // the mirror rather than from `ratings`, because the mirror is the copy that exists for a
    // player who has not signed in since this process started.
    const mirror = { rating: c.mmr, matches: c.rated, wins: c.wins, losses: c.losses,
                     progress: c.progress };
    const rank = progressLib.publicProgress(mirror, { top: isReaper(id, c.progress) });
    const live = reports.get(id) || null;
    const presence = directoryPresence ? directoryPresence(id) : {
      status: statusOf(id), online: bySteam.has(id), queue_mode: isQueued(id) ? mode.id : '',
    };
    const ratio = (a, b) => (b > 0 ? Math.round((a / b) * 100) / 100 : (a > 0 ? a : 0));
    return {
      player_id: id,
      persona: c.persona || '',
      account_type:'Unknown', account_id:'', account_created:0, steam_login_id:'', linked_steam_id:'',
      game_steam_id: verifiedPlayers.get(id)?.game_steam_id || c.game_steam_id || '',
      auth_method: c.auth_method || '',
      status: presence.status,
      online: presence.online,
      queue_mode: presence.queue_mode,
      rank_name: rank.placing ? '' : (rank.rank_name || ''),
      division: rank.placing ? null : rank.division,
      placing: rank.placing,
      rr: rank.placing ? null : rank.rr,
      progress: c.progress || 0,
      mmr: c.mmr || 0,
      matches: c.rated || 0,
      wins: c.wins || 0,
      losses: c.losses || 0,
      win_rate: c.rated ? Math.round((c.wins / c.rated) * 100) : 0,
      played: c.played || 0,
      kills: c.kills || 0,
      deaths: c.deaths || 0,
      kd: ratio(c.kills || 0, c.deaths || 0),
      kpr: ratio(c.kills || 0, c.rounds || 0),
      team_kills: c.team_kills || 0,
      clutches: c.clutches || 0,
      rounds: c.rounds || 0,
      seconds: c.seconds || 0,
      abandons: c.abandons || 0,
      no_shows: c.no_shows || 0,
      // The live list is the fuller answer when this process has it; the mirror is what is left
      // for everybody else, and it was written by the same code that filled the list.
      reports: live ? live.length : (c.reports_in || 0),
      reporters: live ? new Set(live.map((r) => r.by)).size : (c.reporters || 0),
      reports_made: c.reports_out || 0,
      cheater_score: null,
      suspicion: { available: false, status: 'unavailable' },
      banned: Boolean(ban),
      ban_until: ban ? (ban.until || 0) : 0,
      ban_reason: ban ? (ban.reason || '') : '',
      sessions: c.sessions || 0,
      first_seen: c.first_seen || 0,
      last_seen: c.last_seen || 0,
      last_match: c.last_match || 0,
      last_map: c.last_map || '',
      hub: c.hub || '',
      mode: c.mode || '',
      admin: isAdmin(id),
    };
  }

  /**
   * THE PLAYER SEARCH the console draws (Sam, 2026-09-16).
   *
   * Filtered, sorted and paged HERE rather than in the browser. A page that filters what it was
   * sent can only ever search what it was sent, which is the bug where a moderator types a name,
   * gets nothing, and concludes the player does not exist.
   *
   * THE COLUMNS ARE NOT THE PAGE'S BUSINESS EITHER. Every row carries every field; which of them
   * are drawn is a question for the person looking, and the answer lives in their presets. That
   * means turning a column on never costs a request.
   */
  async function accountPopulation() {
    await loadRoster();
    await adoptLadder();
    // Whoever this process has touched but the directory has not heard of yet: somebody who
    // signed in before this shipped and has not since, a banned account, a reported one.
    for (const id of [...bySteam.keys(), ...ratings.keys(), ...bans.keys(), ...reports.keys()]) {
      if (identity.validPlayer(String(id)) && !careers.has(String(id))) {
        careers.set(String(id), blankCareer(id));
      }
    }
    const inventory = accountDirectory?.snapshot() || {available:false, stale:false, updated_at:null, rows:[]};
    const snapshot = {...inventory, available:inventory.available && (!store || (rosterAvailable && ladderAdopted))};
    const indexed = {...snapshot, byPlayer:new Map(snapshot.rows.map(r=>[r.player_id,r])), reassignedIds:new Set(snapshot.reassigned||[])};
    const ids = new Set([...careers.keys(), ...indexed.byPlayer.keys()]);
    const rows = [...ids].map(id=>require('./admin-accounts.cjs').enrich(directoryRow(id),indexed));
    return {rows,snapshot};
  }

  async function adminAccountSummary(steamId, range = {}) {
    if (!isAdmin(steamId)) return {ok:false,error:'not an admin'};
    const {rows,snapshot} = await accountPopulation();
    return require('./admin-accounts.cjs').summary(rows,snapshot,range);
  }

  async function adminPlayers(steamId, opts = {}) {
    if (!isAdmin(steamId)) return { ok: false, error: 'not an admin' };
    let reviewEvidence=null;
    if(opts.include_suspicion!==false){
      await loadReportIndex();
      try{reviewEvidence=await analytics?.suspicionEvidence?.();}catch{reviewEvidence={available:false};}
    }
    const population = await accountPopulation();

    const q = String(opts.q || '').trim().toLowerCase().slice(0, 64);
    const limit = Math.max(1, Math.min(DIRECTORY_MAX, Number(opts.limit) || DIRECTORY_PAGE));
    const column = PLAYER_COLUMNS.find((c) => c.key === String(opts.sort || '') && !c.noSort)
      || null;
    const sort = column ? column.key : 'last_seen';
    const dir = String(opts.dir || '').toLowerCase() === 'asc' ? 'asc' : 'desc';

    let rows = population.rows;
    const accounts = require('./admin-accounts.cjs').summary(rows,population.snapshot);
    const total = rows.length;
    if (opts.player_id) rows = rows.filter(r=>r.player_id === String(opts.player_id));
    if (q) {
      // Name OR id, which is the whole ask - and the id matches on any part of it, because the
      // thing a moderator has in front of them is as often the last four digits off a screenshot
      // as it is the whole number.
      rows = rows.filter((r) => [r.player_id,r.account_id,r.steam_login_id,r.game_steam_id,r.persona]
        .some(value=>String(value||'').toLowerCase().includes(q)));
    }
    const found = rows.length;
    if(opts.include_suspicion!==false){
      const evaluatedAt=Date.now();
      for(const row of rows){
        row.suspicion=require('./suspicion.cjs').score({player:row.player_id,alerts:reviewEvidence?.byPlayer?.get(row.player_id)||[],reports:reports.get(row.player_id)||[],now:evaluatedAt,
          available:Boolean(reviewEvidence?.available)&&reportIndexAvailable&&!reportsUnavailable.has(row.player_id),complete:reviewEvidence?.complete!==false});
        row.cheater_score=row.suspicion.available?row.suspicion.score:null;
      }
    }
    const at = (r) => {
      const v = r[sort];
      return v === null || v === undefined ? (column && column.text ? '' : -1) : v;
    };
    rows.sort((a, b) => {
      const x = at(a);
      const y = at(b);
      let cmp;
      if (typeof x === 'string' || typeof y === 'string') {
        cmp = String(x).toLowerCase().localeCompare(String(y).toLowerCase());
      } else {
        cmp = (Number(x) || 0) - (Number(y) || 0);
      }
      // A stable tiebreak, so two players with the same figure do not swap places every refresh.
      if (!cmp) cmp = a.player_id.localeCompare(b.player_id);
      return dir === 'asc' ? cmp : -cmp;
    });
    rows = rows.slice(0, limit);

    // NAMES LAST, AND ONLY FOR THE ROWS GOING OUT. An account adopted from the leaderboard has
    // no name in the directory yet, and loadProfiles is one read per player: doing it for every
    // known account would be a thousand reads to draw fifty rows. Done here it is bounded by the
    // page, it writes the name back into the directory, and so each one is paid once ever.
    const unnamed = rows.filter((r) => !r.persona).map((r) => r.player_id);
    if (unnamed.length) {
      await loadProfiles(unnamed.slice(0, PROFILE_BACKFILL));
      for (const row of rows) {
        if (row.persona) continue;
        const name = (profiles.get(row.player_id) || {}).persona || '';
        if (!name) continue;
        row.persona = name;
        saveCareer(row.player_id, { ...careerOf(row.player_id), persona: name });
      }
    }

    return {
      ok: true,
      now: Date.now(),
      columns: PLAYER_COLUMNS,
      accounts,
      corrections:opts.include_suspicion===false?[]:await restitution.statuses().then(jobs=>jobs.map(j=>({player_id:j.decision.player_id,status:j.status,scanned:j.scanned,matched:j.matched,error:j.error}))).catch(()=>null),
      rows,
      // What the footer says, and the three numbers are three different questions: how many
      // accounts there are, how many the search matched, and how many are on this page.
      total,
      found,
      shown: rows.length,
      q: String(opts.q || '').trim().slice(0, 64),
      sort,
      dir,
      limit,
      // A directory held only in this process's memory is a directory that forgets on redeploy,
      // and the page says so rather than quietly showing a short list.
      persisted: Boolean(store),
    };
  }

  // ---------------------------------------------------------------- moving a rank by hand
  //
  // Sam, 2026-09-16: "certain buttons that we can press for each user such as ban/unban/reset
  // rank/change elo/change rank".
  //
  // THESE ARE THE ONLY WRITES THE CONSOLE CAN MAKE TO A LADDER, and each of them is one decision
  // a person made rather than a slider. They go through saveRating like every other rating write
  // - which is what keeps the leaderboard index, the directory mirror and the player's own badge
  // in step with each other - and every one of them is logged with the admin who did it.
  //
  // THE TWO LADDERS ARE MOVED SEPARATELY, deliberately. MMR is what the matchmaker reads and RR
  // is what the player sees (docs/ranks.md), and an admin fixing a visible rank after a bad night
  // is not saying anything about how strong the player is. A button that quietly moved both would
  // make every correction a matchmaking change nobody asked for.

  /** Tell them, if they are here. The badge on their hero moves now rather than the next time
   *  they restart the hub - which for a player who has just been handed a rank is the difference
   *  between a fix and a rumour. Exactly the event handleStream sends on connect. */
  function pushRating(steamId, record) {
    sendTo(steamId, { type: 'rating',
                      ...progressLib.publicProgress(record,
                                                    { top: isReaper(steamId, record.progress) }),
                      matches: record.matches, wins: record.wins, losses: record.losses });
  }

  /** The three of them start the same way: an admin, a real account, and the CURRENT record
   *  rather than a default we would otherwise overwrite them with. */
  async function adminTarget(adminId, body) {
    if (!isAdmin(adminId)) return { error: 'not an admin' };
    const target = String((body && (body.player_id || body.steam_id)) || '');
    if (!identity.validPlayer(target)) return { error: 'not a steamid64' };
    return { target };
  }

  /**
   * BACK TO A NEW ACCOUNT'S LADDER. Placements again, no RR, off the leaderboard.
   *
   * It resets the LADDER and nothing else: their matches, their kills, their reports and their
   * bans are all still theirs, because "reset rank" is a decision about a rank and quietly
   * erasing a player's history behind it would be a different, much larger decision.
   */
  async function resetRank(adminId, body) {
    const { error, target } = await adminTarget(adminId, body);
    if (error) return { ok: false, error };
    const fresh = { ...ratingLib.defaultRating(), matches: 0, wins: 0, losses: 0,
                    progress: 0, demoteArmed: false, updated: Date.now() };
    const clean = await mutateRating(target, () => fresh, {actor_id:String(adminId),action:'reset'});
    refreshReaperCut(true);
    pushRating(target, clean);
    console.log('[admin] %s reset the rank of %s', adminId, target);
    return { ok: true, player_id: target, rating: clean.rating, progress: clean.progress };
  }

  /**
   * THE HIDDEN NUMBER, set by hand. What the matchmaker reads, and nothing the player can see.
   *
   * RD IS RESET WITH IT, and that is the whole point of doing this rather than nudging it: an
   * admin who says a player is worth 1400 is making a fresh claim about them, and leaving a
   * deviation of 40 behind would tell Glicko the new figure is forty times more certain than
   * anything it measured itself. It goes back to a starting deviation, so the next few matches
   * are allowed to correct us.
   */
  async function setElo(adminId, body) {
    const { error, target } = await adminTarget(adminId, body);
    if (error) return { ok: false, error };
    const wanted = Number((body && body.rating));
    if (!Number.isFinite(wanted)) return { ok: false, error: 'not a rating' };
    const rating = Math.max(ratingLib.RATING_FLOOR, Math.min(ELO_CEILING, Math.round(wanted)));
    let was;
    const clean = await mutateRating(target, record => {
      was = record.rating;
      return { ...record, rating, rd: ratingLib.START_RD, updated: Date.now() };
    }, {actor_id:String(adminId),action:'elo'});
    // The RR total is NOT touched: the player's visible rank is not this number and never was.
    console.log('[admin] %s set the mmr of %s to %d (was %d)',
                adminId, target, clean.rating, was);
    return { ok: true, player_id: target, rating: clean.rating, was };
  }

  /**
   * THE VISIBLE RANK, set by hand: a rank and a division, or an RR total outright.
   *
   * A PLACING ACCOUNT IS LIFTED OUT OF PLACEMENTS by this, and it has to be. RR is not shown at
   * all until the placement matches are done (rating.isPlacing), so handing a rank to an account
   * with three matches would write a number that nothing draws - the admin would press the button,
   * see no change, and press it again.
   *
   * THE CAPSTONE CANNOT BE GRANTED. Reaper is a seat on the leaderboard held by the highest RR
   * totals in the world, not a band RR can reach (ladder.cjs), so the highest thing this can set
   * is the top division of the last tiered rank. Setting an RR total above the cut makes the
   * account ELIGIBLE for the seat, which is as close as an honest button gets.
   */
  async function setRank(adminId, body) {
    const { error, target } = await adminTarget(adminId, body);
    if (error) return { ok: false, error };
    const D = progressLib.DIVISIONS;
    let progress;
    if (body && body.progress !== undefined && body.progress !== null && body.progress !== '') {
      const total = Number(body.progress);
      if (!Number.isFinite(total) || total < 0) return { ok: false, error: 'not an rr total' };
      progress = Math.round(total);
    } else {
      const rank = Math.round(Number((body && body.rank)));
      const division = Math.round(Number((body && body.division)) || 1);
      if (!Number.isFinite(rank) || rank < 1 || rank > progressLib.TIERED_RANKS) {
        return { ok: false, error: `rank must be 1 to ${progressLib.TIERED_RANKS}` };
      }
      if (!Number.isFinite(division) || division < 1 || division > D) {
        return { ok: false, error: `division must be 1 to ${D}` };
      }
      // The BOTTOM of that division, so the badge reads exactly what was asked for rather than
      // sitting one RR off it.
      progress = (rank - 1) * D * progressLib.RR_PER_DIVISION
        + (division - 1) * progressLib.RR_PER_DIVISION;
    }
    const clean = await mutateRating(target, record => ({ ...record, progress,
      matches: Math.max(record.matches, ratingLib.PLACEMENT_MATCHES), demoteArmed: false }), {actor_id:String(adminId),action:'rank'});
    refreshReaperCut(true);
    pushRating(target, clean);
    const now = progressLib.publicProgress(clean, { top: false });
    console.log('[admin] %s set the rank of %s to %s %s (%d rr)',
                adminId, target, now.rank_name, now.division || '', clean.progress);
    return { ok: true, player_id: target, progress: clean.progress,
             rank_name: now.rank_name, division: now.division };
  }

  // ---------------------------------------------------------------- friends
  //
  // Three sets per player and one code. Everything symmetric is written to BOTH sides, because a
  // friendship that exists in one direction is the bug that makes a list look haunted: they can
  // see you, you cannot see them, and nothing in the data says which side is wrong.
  function setOf(map, steamId) {
    let set = map.get(steamId);
    if (!set) { set = new Set(); map.set(steamId, set); }
    return set;
  }

  async function loadFriends(steamId) {
    if (friendsLoaded.has(steamId) || !store) return;
    friendsLoaded.add(steamId);
    const read = async (key, map) => {
      try {
        const raw = await store(['SMEMBERS', key]);
        if (Array.isArray(raw)) for (const id of raw) setOf(map, steamId).add(String(id));
      } catch { /* an unreadable set is an empty one; nothing here is load-bearing enough to throw */ }
    };
    await Promise.all([
      read(friendsKey(steamId), friends),
      read(reqInKey(steamId), reqIn),
      read(reqOutKey(steamId), reqOut),
    ]);
  }

  function writeSet(key, steamId, map, id, add) {
    const set = setOf(map, steamId);
    if (add) set.add(id); else set.delete(id);
    if (!store) return;
    Promise.resolve(store([add ? 'SADD' : 'SREM', key, id]))
      .catch(() => { /* in-memory still serves this process */ });
  }

  function mintFriendCode() {
    const bytes = crypto.randomBytes(FRIEND_CODE_LEN);
    let out = '';
    for (let i = 0; i < FRIEND_CODE_LEN; i += 1) {
      out += PARTY_CODE_ALPHABET[bytes[i] % PARTY_CODE_ALPHABET.length];
    }
    // Grouped for reading aloud, the same reason the alphabet has no I/O/0/1.
    const half = Math.ceil(FRIEND_CODE_LEN / 2);
    return `${out.slice(0, half)}-${out.slice(half)}`;
  }

  /** What a person actually types: lower case, spaces, missing or extra dashes. '' if malformed. */
  function normaliseFriendCode(text) {
    const cleaned = String(text || '').toUpperCase().split('')
      .filter((c) => PARTY_CODE_ALPHABET.includes(c)).join('');
    if (cleaned.length !== FRIEND_CODE_LEN) return '';
    const half = Math.ceil(FRIEND_CODE_LEN / 2);
    return `${cleaned.slice(0, half)}-${cleaned.slice(half)}`;
  }

  /** The player's friend code, minted on first ask and stable until they refresh it. */
  async function friendCode(steamId) {
    const id = String(steamId || '');
    if (friendCodes.has(id)) return friendCodes.get(id);
    if (store) {
      try {
        const raw = await store(['GET', codeKey(id)]);
        if (raw) {
          const code = String(raw);
          friendCodes.set(id, code);
          codeOwners.set(code, id);
          return code;
        }
      } catch { /* mint a new one below */ }
    }
    return await refreshFriendCode(id);
  }

  /**
   * A new code. The OLD one stops working immediately - that is the point of the button: someone
   * who read their code out on stream needs it to stop being an inbox, and a code that kept
   * working for a grace period would keep the problem it was pressed to solve.
   */
  async function refreshFriendCode(steamId) {
    const id = String(steamId || '');
    const old = friendCodes.get(id);

    // CLAIMED WITH SET NX, not with a lookup-then-write. Checking `codeOwners` alone was worse
    // than useless: that map only holds codes this process has touched since it started, so after
    // a redeploy it is nearly empty and every check passes. Two processes minting at the same
    // moment would not see each other either. NX makes the claim itself the check - whoever gets
    // there first owns it, and the loser simply tries again.
    let code = '';
    for (let tries = 0; tries < 8 && !code; tries += 1) {
      const candidate = mintFriendCode();
      if (codeOwners.has(candidate)) continue;
      if (store) {
        let claimed = null;
        try { claimed = await store(['SET', codeOwnerKey(candidate), id, 'NX']); } catch { claimed = null; }
        if (!claimed) continue;                 // somebody already owns it, or the store is down
      }
      code = candidate;
    }
    // Eight failures in a row against a space this size means the store is unreachable, not that
    // we were unlucky. Fall back to an unclaimed code so the player still has one to show; it is
    // in memory for this process and will be re-claimed on the next refresh.
    if (!code) code = mintFriendCode();

    if (old) {
      codeOwners.delete(old);
      if (store) Promise.resolve(store(['DEL', codeOwnerKey(old)])).catch(() => {});
    }
    friendCodes.set(id, code);
    codeOwners.set(code, id);
    if (store) Promise.resolve(store(['SET', codeKey(id), code])).catch(() => {});
    return code;
  }

  async function ownerOfCode(code) {
    const clean = normaliseFriendCode(code);
    if (!clean) return '';
    if (codeOwners.has(clean)) return codeOwners.get(clean);
    if (!store) return '';
    try {
      const raw = await store(['GET', codeOwnerKey(clean)]);
      if (raw) {
        const id = String(raw);
        codeOwners.set(clean, id);
        return id;
      }
    } catch { /* an unknown code is simply unknown */ }
    return '';
  }

  /** Nudge both sides to re-fetch. The list itself is a GET, so this is a notification and not a
   *  payload - the same shape history uses with `history_stale`. */
  function friendsChanged(...ids) {
    for (const id of new Set(ids.filter(Boolean))) sendTo(id, { type: 'friend_update' });
  }

  async function durableFriend(me,target,action){
    try{const result=await store(['EVAL',require('./friend-relationships.cjs').CHANGE,'8',friendsKey(me),friendsKey(target),reqInKey(me),reqOutKey(me),reqInKey(target),reqOutKey(target),(prefix||'hub:')+'message:blocks:'+me,(prefix||'hub:')+'message:blocks:'+target,me,target,action,String(MAX_FRIENDS),String(MAX_FRIEND_REQUESTS)],{strict:true});
      if(!Array.isArray(result))throw Error('Invalid relationship result');const status=result[0];if(status==='error')return {ok:false,error:result[1]};
      if(status==='accepted'||status==='friends'){setOf(friends,me).add(target);setOf(friends,target).add(me);for(const map of [reqIn,reqOut]){setOf(map,me).delete(target);setOf(map,target).delete(me);}}
      if(status==='sent'||status==='pending'){setOf(reqOut,me).add(target);setOf(reqIn,target).add(me);}
      if(status==='declined'){setOf(reqIn,me).delete(target);setOf(reqOut,target).delete(me);}
      if(status==='cancelled'){setOf(reqOut,me).delete(target);setOf(reqIn,target).delete(me);}
      if(status==='removed'){setOf(friends,me).delete(target);setOf(friends,target).delete(me);}
      if(status==='sent')sendTo(target,{type:'friend_request',from:{player_id:me,persona:personaOf(me)}});
      friendsChanged(me,...(status==='declined'?[]:[target]));return {ok:true,...(['friends','pending'].includes(status)?{already:status}:{[status]:true})};
    }catch{return {ok:false,error:'Friend storage unavailable.'};}
  }

  /**
   * Send a friend request, by code or by steam id.
   *
   * IF THEY HAVE ALREADY ASKED YOU, THIS ACCEPTS instead of queuing a mirror-image request. Two
   * people pressing Add at the same time is not an error state and should not need a third press
   * from either of them.
   */
  async function requestFriend(meId, body) {
    const me = String(meId || '');
    if (!identity.validPlayer(me)) return { ok: false, error: 'not a steamid64' };
    let target = String((body && body.target) || '');
    if (body && body.code) target = await ownerOfCode(body.code);
    if (!identity.validPlayer(target)) return { ok: false, error: 'no such friend code' };
    if (target === me) return { ok: false, error: 'that is your own code' };
    if(store)return durableFriend(me,target,'request');

    await Promise.all([loadFriends(me), loadFriends(target)]);
    if (setOf(friends, me).has(target)) return { ok: true, already: 'friends' };
    if (setOf(friends, me).size >= MAX_FRIENDS) return { ok: false, error: 'your friends list is full' };
    if (setOf(friends, target).size >= MAX_FRIENDS) return { ok: false, error: 'their friends list is full' };

    // they already asked us: accept rather than cross requests
    if (setOf(reqIn, me).has(target)) return acceptFriend(me, { target });

    if (setOf(reqOut, me).has(target)) return { ok: true, already: 'pending' };
    if (setOf(reqIn, target).size >= MAX_FRIEND_REQUESTS) {
      return { ok: false, error: 'they have too many pending requests' };
    }
    writeSet(reqOutKey(me), me, reqOut, target, true);
    writeSet(reqInKey(target), target, reqIn, me, true);
    sendTo(target, { type: 'friend_request', from: { player_id: me, persona: personaOf(me) } });
    friendsChanged(me, target);
    return { ok: true, sent: true };
  }

  async function acceptFriend(meId, body) {
    const me = String(meId || '');
    const target = String((body && body.target) || '');
    if (!identity.validPlayer(me) || !identity.validPlayer(target)) return { ok: false, error: 'not a steamid64' };
    if(store)return durableFriend(me,target,'accept');
    await Promise.all([loadFriends(me), loadFriends(target)]);
    if (!setOf(reqIn, me).has(target)) return { ok: false, error: 'no request from them' };
    if (setOf(friends, me).size >= MAX_FRIENDS) return { ok: false, error: 'your friends list is full' };

    writeSet(reqInKey(me), me, reqIn, target, false);
    writeSet(reqOutKey(target), target, reqOut, me, false);
    // BOTH sides, always. A one-sided friendship is the bug that makes a list look haunted.
    writeSet(friendsKey(me), me, friends, target, true);
    writeSet(friendsKey(target), target, friends, me, true);
    friendsChanged(me, target);
    return { ok: true, accepted: true };
  }

  async function declineFriend(meId, body) {
    const me = String(meId || '');
    const target = String((body && body.target) || '');
    if (!identity.validPlayer(me) || !identity.validPlayer(target)) return { ok: false, error: 'not a steamid64' };
    if(store)return durableFriend(me,target,'decline');
    await Promise.all([loadFriends(me), loadFriends(target)]);
    writeSet(reqInKey(me), me, reqIn, target, false);
    writeSet(reqOutKey(target), target, reqOut, me, false);
    // The decliner is told; the sender is not. Telling them turns a quiet no into a notification,
    // and the only thing that can be done with it is ask again.
    friendsChanged(me);
    return { ok: true, declined: true };
  }

  async function cancelFriendRequest(meId, body) {
    const me = String(meId || '');
    const target = String((body && body.target) || '');
    if (!identity.validPlayer(me) || !identity.validPlayer(target)) return { ok: false, error: 'not a steamid64' };
    if(store)return durableFriend(me,target,'cancel');
    await Promise.all([loadFriends(me), loadFriends(target)]);
    writeSet(reqOutKey(me), me, reqOut, target, false);
    writeSet(reqInKey(target), target, reqIn, me, false);
    friendsChanged(me, target);
    return { ok: true, cancelled: true };
  }

  async function removeFriend(meId, body) {
    const me = String(meId || '');
    const target = String((body && body.target) || '');
    if (!identity.validPlayer(me) || !identity.validPlayer(target)) return { ok: false, error: 'not a steamid64' };
    if(store)return durableFriend(me,target,'remove');
    await Promise.all([loadFriends(me), loadFriends(target)]);
    writeSet(friendsKey(me), me, friends, target, false);
    writeSet(friendsKey(target), target, friends, me, false);
    friendsChanged(me, target);
    return { ok: true, removed: true };
  }

  /** The list, with presence. `online` is free: bySteam already knows who has a live stream. */
  async function friendList(meId) {
    const me = String(meId || '');
    if (!identity.validPlayer(me)) return { ok: false, error: 'not a steamid64' };
    await loadFriends(me);
    // A friend is OFFLINE most of the time - that is what a friends list is for - so this is the
    // screen the remembered names exist for. Load them before the rows are built, since `entry`
    // cannot await.
    const listed = [...setOf(friends, me), ...setOf(reqIn, me), ...setOf(reqOut, me)];
    await loadProfiles(listed);
    const entry = (id) => ({ player_id: id, persona: personaOf(id), avatar: avatarOf(id), online: bySteam.has(id) });
    return {
      ok: true,
      code: await friendCode(me),
      friends: [...setOf(friends, me)].map(entry)
        .sort((a, b) => (Number(b.online) - Number(a.online))
                     || a.persona.localeCompare(b.persona)),
      incoming: [...setOf(reqIn, me)].map(entry),
      outgoing: [...setOf(reqOut, me)].map(entry),
      max: MAX_FRIENDS,
    };
  }

  // ---------------------------------------------------------------- leaderboard
  /**
   * The top `limit` rated players, best first, with the asking player's own standing whether or
   * not they are on that page.
   *
   * WHY IT IS BUILT FROM THE INDEX AND NOT FROM MEMORY. `ratings` only holds the players this
   * process has seen since it started, so a board built from it would be "whoever happened to
   * queue since the last redeploy" - which looks like a leaderboard, sorts like a leaderboard,
   * and is wrong in a way nobody can see.
   */
  async function leaderboard(forSteamId, limit) {
    const want = Math.max(1, Math.min(200, Number(limit) || 50));
    const me = String(forSteamId || '');
    if (!store) {
      // No store: say so rather than inventing a board out of this process's memory.
      return { ok: true, available: false, rows: [], you: null };
    }
    // Somebody is looking at the board, so this is a good moment to re-read the capstone cut
    // every OTHER surface draws from. It is debounced and fire-and-forget: the rows below do
    // not wait for it and do not need it (they have the positions in hand).
    refreshReaperCut();
    let ids = [];
    try {
      // Highest first. Upstash returns a flat [member, score, member, score, ...] for WITHSCORES,
      // so ask without them and read the ratings we already keep per player.
      const raw = await store(['ZREVRANGE', boardKey(), '0', String(want - 1)]);
      ids = Array.isArray(raw) ? raw.map(String) : [];
    } catch {
      return { ok: true, available: false, rows: [], you: null };
    }
    const wanted = ids.concat(me && !ids.includes(me) ? [me] : []);
    try { await loadRatings(wanted); }
    catch { return { ok: true, available: false, rows: [], you: null }; }
    // The top fifty are mostly not standing in the queue when somebody opens the board, and a
    // leaderboard of SteamIDs is the version of this bug people actually saw.
    await loadProfiles(wanted);

    const row = (steamId, rank) => {
      const r = ratingOf(steamId);
      // THE CAPSTONE, DECIDED EXACTLY HERE AND NOWHERE MORE CHEAPLY. Every other surface asks
      // the cached cut; this one already knows the player's position on the board, so it can
      // just look: eligible on RR, and inside the top REAPER_SLOTS. `rank` is 1-based, and for
      // `you` off the page it is the ZCOUNT position, which is the same number.
      const seated = rank >= 1 && rank <= REAPER_SLOTS && boardScore(r) >= REAPER_AT;
      // THE VISIBLE LADDER, the same call the hero and the match result are drawn from. Reading
      // the rank off the matchmaking rating here is what let the board disagree with a player's own
      // badge. `placing` cannot normally be true on a board row - placing players are not indexed
      // - but `you` is built with this too, and they can be.
      const k = progressLib.publicProgress(r, { top: seated });
      const placed = !k.placing;
      const capstone = placed && k.top;
      return {
        rank,
        player_id: steamId,
        persona: personaOf(steamId),
        avatar: avatarOf(steamId),
        // The RR total the board is sorted on. NOT the Glicko-2 rating, which used to be sent
        // here: the hidden number is hidden, and a client had no business being able to read it.
        progress: placed ? boardScore(r) : null,
        matches: r.matches,
        wins: r.wins,
        losses: r.losses,
        win_rate: r.matches ? Math.round((r.wins / r.matches) * 100) : 0,
        rank_name: placed ? (k.rank_name || null) : null,
        division: placed ? k.division : null,
        // 0-99 through the division lower down, and the running count from the counting band
        // up - which is what a leaderboard of the top rank is actually ordered by, and the
        // reason that band stops resetting the figure at all (progress.cjs).
        rr: placed ? k.rr : null,
        counting: placed ? k.counting : false,
        top: capstone,
        is_you: steamId === me,
      };
    };

    const rows = ids.map((id, i) => row(id, i + 1));
    // YOUR OWN STANDING, always - a board you are not on is a board you cannot read yourself
    // into. Off the page it needs a real position, which is one more query rather than a guess.
    let you = rows.find((r) => r.is_you) || null;
    if (!you && me) {
      const mine = ratingOf(me);
      if (!ratingLib.isPlacing(mine)) {
        let place = 0;
        try {
          const position = await store(['ZREVRANK', boardKey(), me], { strict: true });
          place = position === null || position === undefined ? 0 : Number(position) + 1;
        } catch { place = 0; }
        you = row(me, place);
      }
    }
    return { ok: true, available: true, rows, you, total: rows.length };
  }

  // ---------------------------------------------------------------- reports
  function loadReportIndex() {
    if (!store) return Promise.resolve();
    if(reportIndexRead&&reportIndexAt&&Date.now()-reportIndexAt>60000)reportIndexRead=null;
    if (!reportIndexRead) {
      reportIndexRead = (async () => {
        const keyPrefix = reportKey('');
        let cursor = '0';
        do {
          const page = await store(['SCAN', cursor, 'MATCH', keyPrefix + '*', 'COUNT', '100']);
          if (!Array.isArray(page) || !Array.isArray(page[1])) throw new Error('report scan failed');
          cursor = String(page[0]);
          const ids = page[1].filter((key) => typeof key === 'string' && key.startsWith(keyPrefix))
            .map((key) => key.slice(keyPrefix.length)).filter((id) => identity.validPlayer(id));
          // Bound concurrent reads even if Redis returns more than COUNT keys.
          for (let i = 0; i < ids.length; i += 20) {
            await Promise.all(ids.slice(i, i + 20).map(loadReports));
          }
        } while (cursor !== '0');
        reportIndexAt=Date.now();reportIndexAvailable=true;
      })().catch(() => {
        // Keep this process's reports available, and retry discovery on the next refresh.
        reportIndexRead = null;
        reportIndexAvailable=false;
      });
    }
    return reportIndexRead;
  }

  async function loadReports(steamId) {
    if (!store || reportsLoaded.has(steamId)&&Date.now()-(reportLoadTimes.get(steamId)||0)<60000) return reports.get(steamId) || [];
    try {
      const raw = await store(['GET', reportKey(steamId)],{strict:true});
      if (raw) {
        const parsed = typeof raw === 'string' ? identity.parse(raw) : identity.hydrate(structuredClone(raw));
        if (Array.isArray(parsed)) {
          // Anything written in THIS process while the read was in flight is newer and wins, the
          // same rule loadRating and loadPenalty use.
          const known = reports.get(steamId) || [];
          const seen = new Set(known.map((r) => `${r.by}:${r.match_id}`));
          reports.set(steamId, known.concat(parsed.filter((r) => !seen.has(`${r.by}:${r.match_id}`))));
        }else throw Error('Invalid reports');
      }
      reportsLoaded.add(steamId);reportLoadTimes.set(steamId,Date.now());reportsUnavailable.delete(steamId);
    } catch { reportsLoaded.delete(steamId);reportsUnavailable.add(steamId); }
    return reports.get(steamId) || [];
  }

  function saveReports(steamId, list) {
    reports.set(steamId, list);
    reportsLoaded.add(steamId);
    reportLoadTimes.set(steamId,Date.now());
    if (!store) return;
    Promise.resolve(store(['SET', reportKey(steamId), JSON.stringify(list),
                           'EX', String(REPORT_TTL_SECONDS)]))
      .catch(() => { /* the in-memory copy still serves this process */ });
  }

  /**
   * Report a player. Returns what the reporter should be told, and never more than that.
   *
   * WHO MAY REPORT WHOM. Only someone who was actually in a match with them - checked against the
   * live match if there is one, and against the archive if the match is over (the history screen
   * is one of the places Sam asked for the button). Without that check this is an endpoint for
   * burying a stranger under reports from accounts that never played them.
   *
   * WHAT THE REPORTER IS TOLD: that it was recorded. Not the target's total, not who else has
   * reported them, not whether anything came of it. A count handed back is a tool for deciding
   * whether a pile-on is working.
   */
  async function reportPlayer(reporterId, body) {
    const by = String(reporterId || '');
    const target = String((body && body.target) || '');
    const reason = String((body && body.reason) || '');
    const note = reason === 'other' ? String((body && body.note) || '').trim() : '';
    // Older hubs omit note; retain their existing Other reports during the rollout.
    if (reason === 'other' && body.note !== undefined && !note) return { ok: false, error: 'Please describe what happened.' };
    if (note.length > 1000) return { ok: false, error: 'Please keep the explanation under 1,001 characters.' };
    const matchId = String((body && body.match_id) || '');
    if (!identity.validPlayer(by)) return { ok: false, error: 'not a steamid64' };
    if (!identity.validPlayer(target)) return { ok: false, error: 'that is not a player' };
    if (by === target) return { ok: false, error: 'you cannot report yourself' };
    if (!REPORT_REASONS.includes(reason)) return { ok: false, error: 'unknown reason' };

    // Were they in a match together? The live one first, then the archive.
    let sharedMatch = '';
    const liveId = inMatch.get(by);
    const live = liveId && matches.get(liveId);
    if (live && (!matchId || matchId === live.id)
        && everyone(live).some((p) => p.player_id === target)
        && everyone(live).some((p) => p.player_id === by)) {
      sharedMatch = live.id;
    } else if (matchId) {
      const rec = await readMatch(matchId, by);
      const players = (rec && rec.players) || [];
      if (players.some((p) => p.player_id === by) && players.some((p) => p.player_id === target)) {
        sharedMatch = matchId;
      }
    }
    if (!sharedMatch) return { ok: false, error: 'you were not in that match with them' };

    const list = (await loadReports(target)).slice();
    // One per reporter per match. A second is not a failure worth an error message - the button
    // may simply have been pressed twice - so it is reported as accepted and changes nothing.
    if (list.some((r) => r.by === by && r.match_id === sharedMatch)) {
      return { ok: true, recorded: false, already: true };
    }
    list.unshift({ at: Date.now(), by, reason, match_id: sharedMatch, ...(note ? { note } : {}) });
    saveReports(target, list.slice(0, REPORT_KEEP));
    // Both ends of it, into the directory: what was said about them, and what they have said
    // about other people. A moderator reading an accusation wants both columns.
    noteReport(by, target, list);
    // The whole reason chat is stored at all: a moderator reading this report needs what was
    // said, and the ordinary thirty-day TTL may not reach them. See CHAT_TTL_SECONDS.
    keepChatForReport(sharedMatch);
    console.log('[report] %s reported %s for %s (match %s)', by, target, reason, sharedMatch);
    return { ok: true, recorded: true };
  }

  /**
   * What the admin console reads: every report against one player, plus the shape of them.
   *
   * `reporters` is the count of DISTINCT people, which is the number that means something. Ten
   * reports from one person is one person; three from three is a pattern.
   */
  async function reportsFor(steamId) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return { ok: false, error: 'not a steamid64' };
    const list = await loadReports(id);
    const byReason = {};
    for (const r of list) byReason[r.reason] = (byReason[r.reason] || 0) + 1;
    return {
      ok: true, player_id: id, total: list.length,
      reporters: new Set(list.map((r) => r.by)).size,
      by_reason: byReason,
      last: list.length ? list[0].at : 0,
      reports: list,
    };
  }

  // ---------------------------------------------------------------- bug reports
  async function loadBugs() {
    if (bugsLoaded || !store) return bugs;
    bugsLoaded = true;
    try {
      const raw = await store(['GET', bugsKey()]);
      if (raw) {
        const parsed = typeof raw === 'string' ? identity.parse(raw) : identity.hydrate(structuredClone(raw));
        if (Array.isArray(parsed)) {
          // Anything filed in THIS process while the read was in flight is newer and must not be
          // lost to it - the same rule loadReports and loadRating follow.
          const seen = new Set(bugs.map((b) => `${b.by}:${b.at}`));
          bugs = bugs.concat(parsed.filter((b) => b && !seen.has(`${b.by}:${b.at}`)))
                     .sort((a, b) => (b.at || 0) - (a.at || 0))
                     .slice(0, BUG_KEEP);
        }
      }
    } catch { /* a missing or malformed record is simply a hub nobody has filed against */ }
    return bugs;
  }

  function saveBugs(list) {
    bugs = list;
    bugsLoaded = true;
    if (!store) return;
    Promise.resolve(store(['SET', bugsKey(), JSON.stringify(list), 'EX', String(BUG_TTL_SECONDS)]))
      .catch(() => { /* the in-memory copy still serves this process */ });
  }

  /**
   * File a bug report. Returns what the reporter should be told, and nothing more than that.
   *
   * WHAT IS RECORDED: when, who (the steam id AND the name we know them by), what they wrote, and
   * what they were running. The versions are free - every authenticated request already stamps
   * them (noteVersions) - and "what build was it on" is the first question anyone reading a bug
   * report asks.
   *
   * WHAT IS REFUSED: an empty message, and a second report inside BUG_COOLDOWN_MS. The cooldown
   * answers with how long is left rather than a bare no, so the hub can say it in words.
   *
   * LONG TEXT IS TRUNCATED, NOT REFUSED. Somebody who has just pasted a stack trace into the box
   * must not lose the whole thing to a length limit they had no way of knowing about.
   */
  async function submitBugReport(steamId, body) {
    const by = String(steamId || '');
    if (!identity.validPlayer(by)) return { ok: false, error: 'not a steamid64' };
    const text = String((body && body.text) || '').trim().slice(0, BUG_TEXT_MAX);
    if (!text) return { ok: false, error: 'empty' };

    const now = Date.now();
    const last = bugCooldown.get(by) || 0;
    const since = now - last;
    if (last && since < BUG_COOLDOWN_MS) {
      return { ok: false, error: 'too_fast', retry_after_ms: BUG_COOLDOWN_MS - since };
    }
    bugCooldown.set(by, now);

    const v = versions.get(by) || {};
    const row = { at: now, by, persona: personaOf(by), text,
                  hub: String(v.hub || ''), mode: String(v.mode || '') };
    const list = (await loadBugs()).slice();
    list.unshift(row);
    saveBugs(list.slice(0, BUG_KEEP));
    console.log('[bug] %s (%s) filed %d chars', by, row.persona || '-', text.length);
    return { ok: true, recorded: true };
  }

  /** The newest bug reports, for the admin console. Newest first. */
  async function bugReports(limit) {
    const n = Math.max(1, Math.min(BUG_KEEP, Number(limit) || 50));
    return (await loadBugs()).slice(0, n);
  }

  /**
   * A TEAM KILL, reported by the host's game. Detection is the pak's job; the verdict is ours.
   *
   * Same trust window as every other match-world report: the asker must be the HOST of a match
   * that is already `connecting` or `live`, and both players must be on that match's roster. The
   * worst an outsider can do with it is add a row to a list a human reads.
   *
   * IT RETURNS A VERDICT AND USUALLY DOES NOTHING WITH IT. See TK_ENFORCE.
   */
  async function combatHistory(id) {
    return store ? combatLedger.load(store, prefix || 'hub:', id)
      : combatHistories.get(id) || { version: 1, revision: 0, incidents: [] };
  }

  async function commitCombat(id, previous, next, decision, match) {
    const now = Date.now();
    let receipt = null, rank = null, penalty = null;
    const payload = { expected: previous.revision, history: next,
      authority:{match:match.id,host:reportScope.getStore()?.host||match.host,epoch:reportScope.getStore()?.epoch??match.host_epoch??0} };
    if (decision.classification === 'malicious' && tkEnforcing()) {
      await drainRatingWrites(id);
      await drainPenaltyWrites(id);
      // Read the durable values again under the shared rank lock. CAS below also
      // protects this process from another service instance settling the player.
      const rawRank = store ? await store(['GET', ratingKey(id)], { strict: true }) : null;
      const rawPenalty = store ? await store(['GET', penaltyKey(id)], { strict: true }) : null;
      const before = store ? ratingLib.normalise(rawRank ? identity.parse(rawRank) : {}) : ratingOf(id);
      const priorPenalty = store ? (rawPenalty ? identity.parse(rawPenalty) : {}) : penalties.get(id) || {};
      const count = effectiveCount(priorPenalty) + 1;
      const seconds = rungSeconds(count);
      const hit = progressLib.penalise(before, NO_SHOW_RR);
      rank = { ...before, progress: hit.progress, demoteArmed: hit.delta ? false : before.demoteArmed,
        revision: (before.revision || 0) + 1 };
      penalty = { until: now + seconds * 1000, last: now, reason: 'team_kill', count,
        elo: (priorPenalty.elo || 0) + Math.abs(hit.delta), ruleVersion: decision.ruleVersion,
        decisionId: decision.decisionId, incidentIds: decision.incidentIds, evidence: decision.reasons };
      receipt = { ...penalty, seconds, rr: Math.abs(hit.delta), elo: Math.abs(hit.delta),
        next_seconds: rungSeconds(count + 1), match_id: match.id, rank };
      next = combatLedger.consume(next, decision, now);
      Object.assign(payload, { history: next, receipt, expectedRank: before.revision || 0,
        expectedPenalty: rawPenalty || '', rankJson: JSON.stringify(rank), penaltyJson: JSON.stringify(penalty),
        placing: ratingLib.isPlacing(rank), progress: boardScore(rank) });
    }
    const saved = store ? await combatLedger.commit(store, prefix || 'hub:', id, payload)
      : { history: next, receipt, replayed: false };
    combatHistories.set(id, saved.history);
    if (saved.receipt) {
      // Hydrate only the committed state. Neither optimistic RR nor a notification
      // can precede the atomic evidence/rank/penalty/receipt write.
      const committed = saved.receipt;
      let latestRank = committed.rank, latestPenalty = { ...committed, rank: undefined };
      if (saved.replayed && store) {
        // A receipt is historical. A later sanction or match may already exist.
        const [currentRank, currentPenalty] = await Promise.all([
          store(['GET', ratingKey(id)], { strict: true }), store(['GET', penaltyKey(id)], { strict: true }),
        ]);
        latestRank = currentRank ? identity.parse(currentRank) : null;
        latestPenalty = currentPenalty ? identity.parse(currentPenalty) : null;
        if (!latestRank || !latestPenalty) throw new Error('Committed combat state is unavailable');
      }
      if ((latestRank.revision || 0) >= (ratingOf(id).revision || 0)) {
        ratings.set(id, latestRank); ratingLoaded.add(id); noteRating(id, latestRank);
      }
      penalties.set(id, latestPenalty); penaltyLoaded.add(id);
      if (!saved.replayed) { const { rank: _rank, ...publicPenalty } = committed;
        sendTo(id, { type: 'penalty', ...publicPenalty }); }
    }
    return saved;
  }

  const matchOperations = new Map();
  const approvedClosures = new WeakSet();
  const hostChecks = new Map();
  const operationScope = new (require('node:async_hooks').AsyncLocalStorage)();
  const reportScope = new (require('node:async_hooks').AsyncLocalStorage)();
  let stopping = false;
  function matchOperation(id, operation) {
    if(operationScope.getStore()===id)return Promise.resolve().then(operation);
    if (stopping) return Promise.resolve({ ok: false, error: 'server is restarting',
      data_collected: false, close_allowed: false });
    const pending = (matchOperations.get(id) || Promise.resolve()).catch(() => {}).then(()=>operationScope.run(id,operation));
    matchOperations.set(id, pending);
    pending.finally(() => { if (matchOperations.get(id) === pending) matchOperations.delete(id); }).catch(() => {});
    return pending;
  }

  // Ballots belong to authenticated, frozen game identities, never to a client-supplied count.
  // Six is absolute, including in a private/test match with fewer than ten humans.
  function voidVotePayload(match, recipient) {
    if (duel || !(match.void_votes instanceof Map)) return null;
    const voters = new Set(), counts = { yes: 0, no: 0 };
    for (const [id, yes] of match.void_votes) {
      const game = identity.gameFor(match, id);
      if (!game || voters.has(game) || typeof yes !== 'boolean') continue;
      voters.add(game); counts[yes ? 'yes' : 'no']++;
    }
    return { caller: match.void_caller || '', ...counts, needed: 6,
      voted: match.void_votes.has(recipient), pending: Boolean(match.void_pending) };
  }

  function broadcastVoidVote(match) {
    for (const p of match.players) sendTo(p.player_id,
      { type: 'match_void_vote', match_id: match.id, vote: voidVotePayload(match, p.player_id) });
  }

  function voteToVoid(account, body) {
    if(duel)return {ok:false,error:'Void voting is not available in 1v1.'};
    const id = String(body.match_id || ''), player = account.player_id;
    return matchOperation(id, async () => {
      const match = matches.get(id);
      if (match) {
        try { await refreshAuthority(match); }
        catch { return { ok: false, unavailable: true, error: 'Match state is unavailable. Please retry.' }; }
      }
      if (!match || inMatch.get(player) !== id || match.state !== 'live' ||
          !match.players.some(p => p.player_id === player) ||
          identity.gameFor(match, player) !== account.game_steam_id ||
          !identity.validBindings(match) || (Object.hasOwn(body, 'yes') && typeof body.yes !== 'boolean'))
        return { ok: false, error: 'Only players in this live match can vote.' };
      if (match.void_pending) return finishVoidVote(match);
      if (match.collecting || match.settling || match.final_snapshot || match.terminal || match.void_pending || match.finished)
        return { ok: false, error: 'The match is already finishing.' };
      if (!(match.void_votes instanceof Map)) {
        match.void_votes = new Map();
        match.void_caller = match.players.find(p => p.player_id === player).persona || '';
      }
      if (Object.hasOwn(body, 'yes') && !match.void_votes.has(player)) match.void_votes.set(player, body.yes);
      if (voidVotePayload(match, player).yes >= 6) {
        match.void_pending = Date.now();
        match.final_snapshot = JSON.stringify({ voided: true });
        clearTimeout(match.timer); clearTimeout(match.collectTimer);
        match.timer = match.collectTimer = null;
        return finishVoidVote(match);
      }
      try {
        await persistLive(match);
        broadcastVoidVote(match);
        return { ok: true, match_id: id, vote: voidVotePayload(match, player) };
      } catch { return { ok: false, unavailable: true, error: 'Your vote could not be saved. Please retry.' }; }
    });
  }

  async function voidMatchForBan(match) {
    if (!match || matches.get(match.id) !== match || match.collecting || match.settling || match.finished) return;
    if (match.void_pending) return finishVoidVote(match);
    if (match.final_snapshot) return;
    if (match.state !== 'live') { await closeMatch(match, 'player_banned'); return; }
    match.void_pending = Date.now(); match.void_reason = 'ban';
    match.final_snapshot = JSON.stringify({ voided: true });
    clearTimeout(match.timer); clearTimeout(match.collectTimer);
    match.timer = match.collectTimer = null;
    return finishVoidVote(match);
  }

  function hasVoidDecision(match) {
    return Boolean(match.void_pending && (match.void_reason === 'ban' || (duel && match.void_reason === 'service_failure') || (voidVotePayload(match, '')?.yes || 0) >= 6));
  }

  async function finishVoidVote(match) {
    if (matches.get(match.id) !== match || !hasVoidDecision(match))
      return { ok: false, error: 'Six player votes are required.' };
    try {
      // Save the decision before any completion event. A restart retries this exact void,
      // and final score reports cannot turn it into a rated result while storage is down.
      await persistLive(match);
      if (matches.get(match.id) !== match) {
        const accepted = await readReceipt(match.id);
        return { ok: true, voided: accepted?.voided === true, match_id: match.id };
      }
      if (!hasVoidDecision(match) || match.collecting)
        return { ok: false, error: 'The match is already finishing.' };
      const ids = everyone(match).map(p => p.player_id), now = Date.now(), reason = ['ban','service_failure'].includes(match.void_reason) ? match.void_reason : 'vote';
      const record = { ended: match.void_pending, outcome: 'voided', reason, map: match.map,
        host: match.host, sides: match.sides || {} };
      const full = { ...fullRecord(match, record), voided: true, void_reason: reason, won_team: null,
        score: null, data_collected: true };
      const receipt = { mode:mode.id, version: settlementLib.VERSION, matchId: match.id, match_id: match.id,
        host: match.host, host_epoch: match.host_epoch || 0, at: now, collected_at: now, close_after: now + 5000,
        report_digest: /^[a-f0-9]{64}$/.test(match.reportToken || '')
          ? crypto.createHash('sha256').update(match.reportToken).digest('hex') : null,
        data_collected: true, voided: true, participants: ids, full, publicMatch: full,
        analytics_context: analyticsContext(match), inputs: { teams: match.teams, mm: match.mm },
        board: scoreboardOf(match), rows: [], ratings: {}, history: {}, events: {} };
      const keys = [settlementKey(match.id)], types = ['string'];
      const add = (key, type) => { keys.push(key); types.push(type); return keys.length; };
      const plan = { id: match.id, receipt, types, ttl: MATCH_TTL_SECONDS, history_ttl: HISTORY_TTL_SECONDS,
        keep: HISTORY_KEEP, writes: [], histories: [], board: [], hashes: [], rank_checks: [],
        result_index: add(resultKey(match.id), 'string'), live: add(liveMatchKey(match.id), 'string'),
        live_index: add(liveIndexKey(), 'set'), analytics_outbox: add(`${socialPrefix}analytics:outbox`, 'set'),
        authority: add(authorityKey(match.id), 'string'), host: match.host, host_epoch: match.host_epoch || 0 };
      plan.writes.push({ index: add(matchKey(match.id), 'string'), value: JSON.stringify(full), ttl: MATCH_TTL_SECONDS });
      for (const sid of ids) {
        const row = { ...historyRow(match, record, sid), outcome: 'voided', reason,
          voided: true, won: null, score: null, delta: 0, rr_delta: 0 };
        receipt.history[sid] = row;
        plan.histories.push({ index: add(historyKey(sid), 'list'), value: JSON.stringify(row) });
        receipt.events[sid] = { type: 'match_result', match_id: match.id, voided: true, void_reason: reason,
          won: null, score: null, delta: 0, rr_delta: 0, map: match.map, scoreboard: receipt.board,
          data_collected: true, close_allowed: true, close_after: receipt.close_after };
      }
      const saved = store ? await require('./result-commit.cjs').commit(store, keys, plan) : receipt;
      await acceptCommitted(match, saved);
      return { ok: true, match_id: match.id, voided: saved.voided === true };
    } catch {
      broadcastVoidVote(match);
      return { ok: false, unavailable: true, error: 'The void decision is being saved. Please retry.' };
    }
  }

  async function voidServiceFailure(match) {
    if(!duel||matches.get(match.id)!==match||match.state!=='live')return {ok:false};
    if(match.terminal)return finishDuelDecision(match);
    if(match.final_snapshot)return {ok:false};
    if(match.collecting)match.interrupted_collection=match.collecting;
    delete match.collecting;
    match.void_pending ||= Date.now();match.void_reason='service_failure';
    clearTimeout(match.timer);clearTimeout(match.collectTimer);match.timer=null;match.collectTimer=null;
    return finishVoidVote(match);
  }

  async function checkHostTimeouts() {
    if(!store)return;
    for(const m of matches.values()) {
      if(m.state!=='live'||m.terminal||m.void_pending||!m.migration_capabilities||Date.now()-(hostChecks.get(m.id)||0)<10000)continue;
      hostChecks.set(m.id,Date.now());
      await matchOperation(m.id,async()=>{
        await refreshAuthority(m);
        await syncRecoveryRoster(m);
        if(m.terminal||matches.get(m.id)!==m)return;
        const raw=await store(['GET',authorityKey(m.id)],{strict:true});
        const a=raw&&identity.parse(raw);
        if(a?.phase==='restoring'&&a.restore_until&&Date.now()>=a.restore_until) {
          const expired=await store(['EVAL',require('./recovery.cjs').EXPIRE,'2',authorityKey(m.id),settlementKey(m.id),
            m.host,String(m.host_epoch||0),String(Date.now()),String(LIVE_STATE_TTL_SECONDS)],{strict:true});
          if(expired===1){approvedClosures.add(m);closeMatch(m,'recovery_expired',[]);}
          return;
        }
        if(a?.phase==='restoring')return; // Separate setup/rejoin/verification deadline.
        if(!a?.last_seen||Date.now()-a.last_seen<300000)return;
        // A verified final snapshot remains retryable even if its reporter died.
        if(m.final_snapshot) {
          const f=JSON.parse(m.final_snapshot),[round,limit,s0,s1]=f.meta;
          await applyFinalSnapshot(m.host,{match_id:m.id,meta:`${round};${limit};0;${s0};1;${s1}`,
            combat_end:`${m.combat_end.epoch};${m.combat_end.seq}`,
            rows:f.rows.map(r=>`${r.gameSteamId}|k=${r.kills};d=${r.deaths};sp=${r.spawnCount};t=${r.teamId};s=${r.teamScore};a=${r.alive}`).join(',')});
          return;
        }
        if(duel){await voidServiceFailure(m);return;}
        const closed=await store(['EVAL',require('./migration.cjs').CLOSE,'2',authorityKey(m.id),settlementKey(m.id),
          m.host,String(m.host_epoch||0),String(Date.now()),'300000',String(LIVE_STATE_TTL_SECONDS)],{strict:true});
        if(closed!==1)return;
        // No game was restored. Retain the partial decision/evidence, release
        // everyone and never invent a winner or punish the innocent remainder.
        if(m.collecting)m.interrupted_collection=m.collecting;
        delete m.collecting;
        approvedClosures.add(m);closeMatch(m,'stalled',[]);
      });
    }
  }
  function gameReportedCombat(hostId, report, auth = {}) {
    return matchOperation(String(report?.match || ''), () => applyCombat(hostId, report, auth));
  }
  function combatBatch(hostId, matchId, text) {
    return matchOperation(matchId, async () => {
      const match = matches.get(matchId);
      if (!match || match.host !== String(hostId) || inMatch.get(String(hostId)) !== matchId ||
          !['connecting', 'live'].includes(match.state) || match.finished || match.final_snapshot)
        return { ok: false, error: 'combat report is not authorised for a live match' };
      const rows = String(text || '').replace(/\n$/, '').split('\n');
      const events = rows.map(row => combatLib.parseRow(row));
      if (!rows.length || rows.length > 16 || Buffer.byteLength(String(text), 'utf8') > 8192 ||
          rows.some(row => /[^\x20-\x7e]/.test(row) || Buffer.byteLength(JSON.stringify(row), 'utf8') > 8192) ||
          events.some((e, i) => e?.terminal === 1 && i !== events.length - 1))
        return { ok: false, error: 'invalid combat batch' };
      if (!match.combatState) match.combatState = combatLib.createState();
      if (match.combatState.matchId != null && (match.combatState.matchId !== match.id || match.combatState.hostId !== identity.gameFor(match, match.host)))
        return { ok: false, error: 'scope_mismatch' };
      const rejected = match.combatState.rejected ||= [];
      const rejectedBefore = rejected.length;
      const batchHash = crypto.createHash('sha256').update(String(text || '')).digest('hex');
      if (match.combat_end?.batch_hash === batchHash) {
        await saveCombat(match, match.combat_end);
        return { ok: true, rejected: rejected.length, new_rejected: 0 };
      }
      let terminalEnd = null;
      for (let i = 0; i < rows.length; i++) {
        const fingerprint = crypto.createHash('sha256').update(rows[i]).digest('hex');
        if (rejected.some(row => row.hash === fingerprint)) continue;
        if (match.combat_end && (!events[i] || events[i].epoch !== match.combat_end.epoch || events[i].seq > match.combat_end.seq))
          return { ok: false, error: 'combat capture is already closed' };
        const duplicate = events[i] && Object.hasOwn(match.combatState.seen, events[i].seq);
        if (!duplicate && events[i]?.terminal !== 1 &&
            rejected.length + match.combatState.events.length >= combatLib.POLICY.maxEvents - 1) {
          // Reserve the final marker. Retain one bounded overflow diagnostic rather
          // than counting retries as new observations or blocking the entire queue.
          match.combatState.overflow ||= { hash: fingerprint, raw: rows[i], reason: 'capture_limit' };
          match.combatState.coverage.broken = true;
          continue;
        }
        const result = events[i] ? await applyCombat(hostId, { match: matchId, row: rows[i] },
          { authenticated: true, deferPersistence: true }) : { ok: false, error: 'malformed' };
        if (!result.ok) {
          if (!['malformed', 'sequence_conflict', 'old_sequence', 'invalid_context',
                'actual teams do not agree with the roster'].includes(result.error)) return result;
          if (match.combat_end)
            return { ok: false, error: 'combat capture cannot admit another sample' };
          if (rejected.length + match.combatState.events.length >= combatLib.POLICY.maxEvents - 1) {
            match.combatState.overflow ||= { hash: fingerprint, raw: rows[i], reason: 'capture_limit' };
            match.combatState.coverage.broken = true;
            continue;
          }
          // A captured sample may lack a target or contain an unclamped health value.
          // Preserve it verbatim as ineligible evidence; never hold every later sample
          // and the final marker hostage to the validity of that one observation.
          rejected.push({ hash: fingerprint, raw: rows[i], reason: result.error });
          match.combatState.coverage.broken = true;
        } else if (result.terminal) terminalEnd = { ...result.terminal, batch_hash: batchHash };
      }
      await saveCombat(match, terminalEnd);
      return { ok: true, rejected: rejected.length, new_rejected: rejected.length - rejectedBefore };
    });
  }
  async function applyCombat(hostId, report, auth = {}) {
    const match = matches.get(String(report && report.match || ''));
    const event = typeof report?.row === 'string' ? combatLib.parseRow(report.row) : report?.row;
    const opening = ['coverage', 'phase'].includes(event?.kind);
    if (auth.authenticated !== true || !match || match.host !== String(hostId) ||
        inMatch.get(String(hostId)) !== match.id ||
        !(match.state === 'live' || (match.state === 'connecting' && opening)) || match.finished || match.final_snapshot)
      return { ok: false, error: 'combat report is not authorised for a live match' };
    // The durable final sequence closes admission. Lost acknowledgements can replay
    // that exact batch, but cannot append evidence after the declared end marker.
    if (match.combat_end && (!event || event.epoch !== match.combat_end.epoch || event.seq > match.combat_end.seq))
      return { ok: false, error: 'combat capture is already closed' };
    if (!match.combatState) match.combatState = combatLib.createState();
    const build = event && event.kind === 'coverage' ? event.observer : match.combatState.coverage.observer;
    const allowed = String(process.env.COMP_COMBAT_VALIDATED_COLLECTORS || '').split(',').map(x => x.trim());
    const validated = Boolean(build && allowed.includes(build));
    const agreement = teamsAgree(match);
    if (!agreement.agree && !opening) { match.combatState.coverage.broken = true;
      return { ok: false, error: 'actual teams do not agree with the roster' }; }
    // Opening coverage can precede the slower verified-team sweep. It contains no
    // harm and grants no sanction; every health observation still requires agreement.
    const roster = Object.fromEntries(match.players.map(p => [identity.gameFor(match, p.player_id),
      agreement.agree ? match.ingame.get(p.player_id) : teamOf(match, p.player_id) - 1]));
    const ingested = combatLib.ingest(match.combatState, report.row, {
      authenticated: true, validated, observationOnly: !validated, validatedShot: false, authorityEpoch:match.host_epoch||0,
      hostId: identity.gameFor(match, match.host), matchId: match.id, now: Date.now(), roster, phase: event && event.phase,
    });
    if (['malformed', 'sequence_conflict', 'old_sequence', 'scope_mismatch', 'invalid_context', 'event_limit'].includes(ingested.reason))
      return { ok: false, error: ingested.reason };
    const terminalEnd = event?.kind === 'coverage' && event.terminal === 1 && (ingested.accepted || ingested.duplicate)
      ? { epoch: event.epoch, seq: event.seq, gaps: event.gaps, complete: event.complete === 1 } : null;
    if (auth.deferPersistence) return { ok: true, accepted: ingested.accepted, duplicate: ingested.duplicate, terminal: terminalEnd };
    const saved = await saveCombat(match, terminalEnd);
    return { ok: true, accepted: ingested.accepted, duplicate: ingested.duplicate,
      reason: ingested.reason, ...saved };
  }

  async function saveCombat(match, terminalEnd) {
    let enforced = false, decision = null;
    // Revisit existing actors on a retry too: an accepted event followed by a failed
    // database write must not disappear merely because its sequence is now a duplicate.
    const incidents = match.combatState.incidents.map(i => ({...i,
      gameActorId:i.actorId, actorId:identity.playerFor(match,i.actorId),
      victimIds:i.victimIds.map(id=>identity.playerFor(match,id)),
      lethalVictimIds:(i.lethalVictimIds||[]).map(id=>identity.playerFor(match,id))}));
    if (incidents.some(i=>!i.actorId||i.victimIds.some(id=>!id))) throw Error('Invalid combat ownership');
    const actors = [...new Set(incidents.map(i => i.actorId))];
    for (const actor of actors) await withRatingLocks([actor], async () => {
      for (let attempt = 0; attempt < 4; attempt++) {
        const previous = await combatHistory(actor);
        const next = combatLedger.merge(previous, incidents.filter(i => i.actorId === actor), Date.now());
        if (next.warning && next.warning.at < Date.now() - combatLedger.KEEP_MS) delete next.warning;
        // Receipt time cannot establish capture order: queued attacks may predate
        // a visibly delivered warning. Keep delivery receipts for review, but
        // never use them as intent corroboration without a validated clock bound.
        for (const incident of next.incidents) delete incident.warningAckAt;
        const ruling = combatLib.evaluate(next.incidents, Date.now());
        if (ruling.classification === 'warning' && (!next.warning || next.warning.matchId !== match.id || next.warning.at < Date.now() - combatLedger.KEEP_MS))
          next.warning = { id: crypto.randomBytes(16).toString('hex'), at: Date.now(), matchId: match.id };
        try {
          const saved = await commitCombat(actor, previous, next, ruling, match);
          if (saved.receipt) enforced = true;
          if (ruling.classification !== 'insufficient') decision = ruling;
          if (saved.history.warning && !saved.history.warning.ackAt)
            sendTo(actor, { type: 'team_kill_warning', match_id: match.id,
              warning_id: saved.history.warning.id, enforced: Boolean(saved.receipt), flags: ['repeated_harm'] });
          break;
        } catch (error) {
          // The write may have committed before its reply was lost. Force strict
          // refreshes before the player can queue with stale rank or conduct state.
          ratingLoaded.delete(actor); penaltyLoaded.delete(actor);
          if (!error.conflict || attempt === 3) throw error;
        }
      }
    });
    const previousEnd = match.combat_end;
    if (terminalEnd) match.combat_end = { ...terminalEnd, complete: terminalEnd.complete && !match.combatState.coverage.broken && !match.combat_migrated };
    try { await persistLive(match); }
    catch (error) {
      if (terminalEnd) {
        if (previousEnd) match.combat_end = previousEnd;
        else delete match.combat_end;
      }
      throw error;
    }
    if (match.combatState.coverage.closed) maybeSettleEarly(match);
    return { enforced, classification: decision?.classification || 'insufficient' };
  }

  async function acknowledgeCombatWarning(id, body) {
    return withRatingLocks([id], async () => {
      for (let attempt = 0; attempt < 4; attempt++) {
        const previous = await combatHistory(id), warning = previous.warning;
        if (!warning || warning.id !== body.warning_id || warning.matchId !== body.match_id)
          return { ok: false, error: 'no matching warning' };
        if (warning.ackAt) return { ok: true };
        const next = { ...previous, revision: previous.revision + 1,
          warning: { ...warning, ackAt: Date.now() } };
        try {
          if (store) await combatLedger.commit(store, prefix || 'hub:', id, { expected: previous.revision, history: next });
          combatHistories.set(id, next); return { ok: true };
        } catch (error) { if (!error.conflict || attempt === 3) throw error; }
      }
    });
  }

  function teamKillReported(hostId, report) {
    const host = String(hostId || '');
    const killer = String((report && report.killer) || '');
    const victim = String((report && report.victim) || '');
    if (!identity.validPlayer(host)) return { ok: false, error: 'not a steamid64' };
    if (!identity.validPlayer(killer)) return { ok: false, error: 'killer is not a steamid64' };
    if (!identity.validPlayer(victim)) return { ok: false, error: 'victim is not a steamid64' };
    // The pak already refuses to send one, but a suicide reaching here must never become a ban.
    if (killer === victim) return { ok: false, error: 'a suicide is not a team kill' };

    const matchId = inMatch.get(host);
    const match = matchId && matches.get(matchId);
    if (!match) return { ok: false, error: 'no match' };
    if (match.final_snapshot) return { ok: true, ignored: true };
    if (match.state !== 'connecting' && match.state !== 'live') {
      return { ok: false, error: 'match is not connecting or live' };
    }
    if (String(match.host || '') !== host) return { ok: false, error: 'not the host' };
    const onRoster = (id) => match.players.some((p) => p.player_id === id);
    if (!onRoster(killer) || !onRoster(victim)) return { ok: false, error: 'not on the roster' };

    const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : null; };
    const team = num(report && report.team);
    const elapsed = num(report && report.elapsed);
    const round = num(report && report.round);
    const alive = [num(report && report.alive0), num(report && report.alive1)];

    if (!Array.isArray(match.teamkills)) match.teamkills = [];
    const mine = match.teamkills.filter((k) => k.killer === killer);

    const flags = [];
    // Warm-up is not a round, and a team kill there means much less: no objective, no stakes,
    // and people genuinely mess about. `round` is 0 until the first round starts (measured).
    if (round !== null && round >= 1 && elapsed !== null
        && elapsed <= TK_EARLY_SECONDS) flags.push('early');
    // The enemy is whichever team the killer is NOT on. Both counts are reported rather than
    // "enemies alive" precisely so this does not have to assume the ids are 0 and 1.
    if (team === 0 || team === 1) {
      const enemiesAlive = alive[team === 0 ? 1 : 0];
      if (enemiesAlive === 0) flags.push('no_enemy');
    }
    if (mine.some((k) => k.victim === victim)) flags.push('repeat');
    if (mine.length + 1 >= TK_MATCH_LIMIT) flags.push('volume');

    const entry = { at: Date.now(), killer, victim, team, elapsed, round,
                    alive0: alive[0], alive1: alive[1], flags };
    match.teamkills.push(entry);
    if (match.teamkills.length > TK_KEEP) match.teamkills = match.teamkills.slice(-TK_KEEP);

    const count = mine.length + 1;
    // Legacy kills lack phase, damage continuity and an independent incident identity.
    // Retain their diagnostic flags, but only combat evidence may classify intent.
    const malicious = false;
    let penalty = null;
    if (malicious && tkEnforcing()) {
      // The SAME ladder a no-show climbs. A player who abandons matches and a player who guns
      // down their own team are doing the same thing to the other nine, and a second ladder would
      // let someone alternate between the two and never climb either.
      penalty = applyNoShow(killer);
      penalty.reason = 'team_kill';
      sendTo(killer, { type: 'penalty', ...penalty, reason: 'team_kill' });
    }
    // Told either way, enforcing or not: the warning Sam asked for is the part that might
    // actually change the behaviour, and it costs nothing to send when no ban is attached.
    if (malicious) {
      sendTo(killer, { type: 'team_kill_warning', match_id: match.id, flags, count,
                       enforced: Boolean(penalty) });
    }
    return { ok: true, match_id: match.id, killer, victim, flags, malicious, count,
             enforced: Boolean(penalty), penalty };
  }

  /**
   * The HOST'S GAME saying "I am in the match" - the authority reportConnected's docstring has been
   * waiting for. Called from the probe handler when GM_BB5 reports from inside the match world.
   *
   * WHY THIS IS THE RIGHT SIGNAL. The joiners' release is timing-critical in a way nothing else in
   * this file is: the lobby pak gets exactly ONE lobby search per launch (BeginPlay fires once per
   * level load and there is no clock in the lobby world to retry with), so a joiner opened before
   * the host's lobby carries CH_MATCH searches an empty Steam and has spent its only shot, silently.
   * Until now the host's hub asserted "I am in" - a button the player pressed, or a guess - which
   * could fire while the game was still on the loading screen. GM_BB5's probe cannot: it runs on a
   * timer inside the match world, so it is proof of arrival rather than a claim about it.
   *
   * THE TRUST TRADE, STATED PLAINLY. /api/probe is unauthenticated and its bearer token is a fixed
   * literal baked into the pak, so this path is reachable by anyone who can POST. It is narrowed to
   * the smallest useful window: the steam id must be the HOST of a match that is ALREADY in
   * `connecting`, a state only an authenticated /api/match/connecting can open. So the worst an
   * outsider can do is release the joiners of a match that was genuinely starting a few seconds
   * early - and only if they know the host's SteamID64. The manual route stays as the fallback for
   * a host whose game never reports (an old pak, no network, the probe server down).
   */
  function gameReportedIn(steamId, event) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return { ok: false, error: 'not a steamid64' };
    const matchId = inMatch.get(id);
    const match = matchId && matches.get(matchId);
    if (!match || (match.state !== 'connecting'&&match.recovery?.phase!=='restoring')) return { ok: false, error: 'no connecting match' };
    if (String(match.host || '') !== id) return { ok: false, error: 'not the host' };
    const player = match.players.find((p) => p.player_id === id);
    const name = String(event || '');

    // ARRIVAL IS THE RELEASE AGAIN (2026-09-16), because the reason it stopped being one is gone.
    //
    // THE JOINER NO LONGER SEARCHES CH_MATCH. GM_CHJoin writes BodycamGI "SessionToJoin (Client)"
    // and the GAME runs the whole funnel itself - FindLobbies({Name: token}, MaxResults=1) ->
    // JoinLobby -> travel. So what a joiner needs to exist is a lobby whose NAME is this match's
    // token, and the host stamps that into "Session Name" BEFORE Parent BeginPlay - so every lobby
    // its game opens carries it, including the one the "listen" travel creates in the match world.
    // CH_MATCH is written eighteen seconds later by GM_BB5 and nothing reads it any more.
    //
    //   ch_lobby_read   t+12 s   the host is in the match world and its lobby is up: measured,
    //                            "1/10" members. That is everything a joiner needs.
    //   ch_lobby_write  t+30 s   GM_BB5's UpdateLobby has stamped CH_MATCH. Kept only as a second
    //                            chance in case the read was lost in transit.
    //
    // WHAT WAITING FOR THE WRITE COST (Sam, 2026-09-16): "none of the joiners are able to hit the
    // 'launch game' button to join until the host's preround timer gets to zero". Eighteen seconds
    // of gate, and then the 40-60 s a joiner's own game takes to boot on top of it, put everybody
    // through the door after the round had started. Releasing on arrival gives those eighteen
    // seconds back and stops the joiners' launch hanging off a GM_BB5 timer that was never about
    // them.
    //
    // WHY IT IS STILL RIGHT TO WAIT FOR ARRIVAL. A joiner gets one search per launch, so releasing
    // before the host's lobby exists spends it on an empty Steam. ch_lobby_read is proof that the
    // lobby is up, sent from inside the match world - which is a fact, not a claim the hub makes.
    if (!player) return { ok: false, error: 'not in the match' };
    if(match.recovery?.phase==='restoring') {
      return (async()=>{
      const result=await recoveryTransition(match,id,match.reportToken,{operation:'opened',epoch:match.host_epoch,session:match.session_key});
      if(!result.ok)return result;
      revokeHostPermit(id);grantJoinPermits(match);
      for(const p of match.players)sendTo(p.player_id,livePayload(match,p.player_id));
      return {ok:true,host:id,event:name,connected:1,total:match.players.length};
      })();
    }
    const releasing = !match.joiners_released;
    // A SECOND CHANCE AT THE FRAME, not at the release. Once the joiners are out there is nothing
    // left to do here - but the t+30 write is a free opportunity to re-send the snapshot to a hub
    // that was reconnecting when the first one went out, and a grey Launch button for a whole
    // match is what the lost frame costs. The release itself stays one-shot.
    if (player.connected && !releasing) {
      broadcastConnectProgress(match);
      return { ok: false, error: 'already in' };
    }

    revokeHostPermit(id);          // they are in; a later lobby load must not travel them back

    // THE RELEASE. Whichever arrival event arrives first does it, and the guard is what stops the
    // t+30 write - and every later report, should ARRIVAL_EVENTS ever grow - from re-releasing a
    // match whose joiners have already been let go.
    //
    // `lobby_stamped` is the field broadcastConnectProgress puts on the wire as `stamped`, which is
    // what the hub greys "Launch game" on. The name is historical now: it means "the host's lobby
    // is up and findable", not "CH_MATCH is on it". It is kept because an older hub reads that key,
    // and so gets this fix without being updated.
    let released = 0;
    if (releasing) {
      match.joiners_released = true;
      match.lobby_stamped = true;
      released = grantJoinPermits(match);
    }

    // AND TELL THEM. reportConnected is what normally broadcasts `stamped`, and it is skipped on
    // this branch because the host is already connected - which is ALWAYS true by the time the
    // write arrives, since the read marked them connected eighteen seconds earlier. Without this
    // the release never reaches the joiner and their Launch button stays grey for the whole match.
    const result = player.connected
      ? (broadcastConnectProgress(match),
         { ok: true, connected: match.players.filter((p) => p.connected).length,
           total: match.players.length })
      : reportConnected(id);
    // `stamped` is the state, not the event: it says the joiners are out, which is what the probe
    // log is read for. `event` is there to say which report did it.
    return { ...result, host: id, match_id: match.id, event: name,
             stamped: Boolean(match.lobby_stamped), released };
  }

  // ---------------------------------------------------------------- the scoreboard
  //
  // THE GAME REPORTS ITS OWN SCORE (Sam, 2026-09-15): "have it so the game data is sent to the
  // hub ... and then whichever team reaches the score limit first, wins."
  //
  // The channel is the one that has been carrying `ch_lobby_read` since yesterday:
  // `SendAttributionEvent` with our URL, landing on /api/probe, which routes it here. What the
  // gamemode reads before sending is `ABodycamGameState::GetTeams()` - an array of
  // `{TeamID, TeamScore, PlayerCount}` - and `GetScoreLimit()`. Both are BlueprintPure, both
  // are in the header dump, and `DA_BB5.ScoringConfig.ScoreLimit` is 7.
  //
  // THE TEAM ANCHOR, and why the report needs one. `SetTeamId` exists in the API but we have
  // never called it (docs/autojoin.md, "never called"), so the game assigns its own teams and
  // the in-game `TeamID` has NO relationship to our team 1 and team 2. A report that only said
  // "team 0 leads 7-3" would be unattributable. So the report also carries the HOST's own
  // in-game TeamID: the host is on the roster, we know which of our sides they are on, and one
  // anchor pins the whole mapping. The other in-game team is the other side by elimination.
  //
  // WHY THE SERVER DECIDES THE WINNER rather than the game telling us. Sam's rule is "whichever
  // team reaches the score limit first" - which is a statement about the SCORE, and the score is
  // a fact the report already carries. Deriving the winner here rather than waiting for a
  // match-ended event means a match still settles if the last report is the one that gets lost,
  // and it needs no delegate binding in the pak (the graph builder has no CreateEvent node kind).
  //
  // TRUST. This is the host's word, and the host is a player in the match. That is the same
  // trade `gameReportedIn` already makes and it is not a new hole, but it is a REAL one: a
  // tampered host can report a score it did not earn. What bounds it is that scores are checked
  // for shape and range, the reporter must BE the host of a LIVE match, and integrity layer 5
  // (server-side anomaly detection, docs/competitive.md) is where a host who always wins 7-0
  // gets caught. Moving this to a majority of clients agreeing is the real fix and is written
  // up in docs/matchmaking.md.

  /** `<hostTeamId>|<teamId>:<score>|<teamId>:<score>` - the shape the pak packs into Platform. */
  const SCORE_FIELD = /^(-?\d{1,3})\|(-?\d{1,3}):(\d{1,3})\|(-?\d{1,3}):(\d{1,3})$/;

  /**
   * A score report from the host's game. Returns what happened, for the probe log.
   *
   * Everything about it is best effort and nothing about it can throw into the probe handler:
   * an unparseable report is a report we ignore, not a match we break.
   */
  // What the GAMEMODE pak stamps onto the lobby as CH_MATCH when it has no real id to stamp:
  // bb5_graphs.py LOBBY_VALUE, a hardcoded test constant that has never been per-match. It comes
  // back on every score, round, stats and kill report as `fields.match`.
  const SEED_MATCH_ID = 'ch-test-4821';

  /** Does this report's echoed match id disagree with the match we are applying it to?
   *
   * The echo is a LATE-REPORT guard - it stops a report from a finished match landing on the next
   * one - and not the authorisation, which is "you are this match's host" and is checked separately
   * by every caller. An ABSENT id has always been accepted, because a pak that stamps nothing can
   * still report; SEED_MATCH_ID is that same "nothing", spelled out by a pak that stamps a
   * placeholder. Treating it as a real id refused 100% of reports and no match could ever settle.
   *
   * A genuinely stamped id is still required to match, so the guard keeps doing its job the moment
   * the pak starts carrying a real one. */
  function echoMismatch(fields, match) {
    const claimed = String((fields && fields.match) || '').trim();
    if (!claimed || claimed === SEED_MATCH_ID) return false;
    return claimed !== match.id;
  }

  function gameReportedScore(steamId, fields) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return { ok: false, error: 'not a steamid64' };
    const matchId = inMatch.get(id);
    const match = matchId && matches.get(matchId);
    if (!match) return { ok: false, error: 'no match' };
    if (match.final_snapshot) return { ok: true, ignored: true };
    // `connecting` is accepted as well as `live`: the last joiner's report and the first score
    // tick can cross, and dropping the score because of a race would lose the match's result.
    if (match.state !== 'live' && match.state !== 'connecting') {
      return { ok: false, error: `match is ${match.state}` };
    }
    if (String(match.host || '') !== id) return { ok: false, error: 'not the host' };
    // ALREADY DECIDED. The game keeps reporting the final scoreline for as long as it sits on the
    // end screen - twice more in the 2026-09-15 match. The result cannot change, so these are
    // acknowledged and dropped rather than re-applied.
    if (match.collecting) {
      const c = match.collecting;
      return { ok: true, score: c.score, limit: c.limit, finished: true, winner: c.winner,
               match_id: match.id, collecting: true };
    }
    // The match id the lobby was stamped with, echoed back. When the pak has it, it must agree:
    // a report from a DIFFERENT match arriving late must not be applied to this one.
    if (echoMismatch(fields, match)) return { ok: false, error: 'wrong match id' };

    const parsed = SCORE_FIELD.exec(String((fields && fields.scores) || '').trim());
    if (!parsed) return { ok: false, error: 'unreadable score' };
    // Groups: 1 host team, 2/3 the first team and its score, 4/5 the second and its score.
    const hostTeamId = Number(parsed[1]);
    const sides = [{ team: Number(parsed[2]), score: Number(parsed[3]) },
                   { team: Number(parsed[4]), score: Number(parsed[5]) }];
    if (sides[0].team === sides[1].team) return { ok: false, error: 'one team twice' };

    // THE ANCHOR. Which of OUR sides is the host on, and which in-game team are they?
    const hostSide = teamOf(match, id);
    if (hostSide !== 1 && hostSide !== 2) return { ok: false, error: 'host has no team' };

    // THE ANCHOR IS NOT READY AT ONCE (measured 2026-09-15, Sam's first hosted launch). The first
    // report of the match came in reading `-1|0:0|1:0`: `ABodycamPlayerState::TeamID` starts at -1
    // and the game had not assigned the host a team yet. It settled to 0 about thirty seconds
    // later. So every report in that opening window named a team that is in no scoreline, and the
    // old code refused them all with 'host team not in the score' - which reads like corruption
    // and is actually just the match not having started.
    //
    // Two changes. An unassigned anchor is now its own answer rather than an error, and once a
    // mapping HAS been learned it is remembered on the match and reused - so a later report whose
    // anchor flickers back to -1 (a host who respawns, or a spectator frame) still lands.
    let map = match.team_map || null;
    const hostEntry = sides.find((s) => s.team === hostTeamId);
    if (hostEntry) {
      const otherEntry = sides.find((s) => s !== hostEntry);
      map = { [hostEntry.team]: hostSide, [otherEntry.team]: hostSide === 1 ? 2 : 1 };
    } else if (!map) {
      return { ok: false, error: 'team anchor not ready', anchor: hostTeamId };
    }

    const score = {};
    for (const s of sides) {
      const side = map[s.team];
      if (side !== 1 && side !== 2) return { ok: false, error: `unmapped in-game team ${s.team}` };
      score[side] = s.score;
    }
    if (score[1] === undefined || score[2] === undefined) {
      return { ok: false, error: 'the two teams did not map onto two sides' };
    }
    const reportedLimit = Number(String((fields && fields.limit) || '').trim());
    const forced = match.start_ready_verified ? match.expected_score_limit : forcedScoreLimit();
    const limit = match.start_ready_verified ? match.expected_score_limit : match.reportToken && match.legacyReportAuth !== true
      ? (match.agreedScoreLimit || DEFAULT_SCORE_LIMIT) : forced !== null ? forced
      : (Number.isFinite(reportedLimit) && reportedLimit > 0
          ? Math.min(999, reportedLimit) : DEFAULT_SCORE_LIMIT);

    return applyScoreline(match, score, limit, map);
  }

  /**
   * ONE SETTLE PATH, whatever saw the score.
   *
   * Split out of gameReportedScore on 2026-09-17 so the DERIVED scoreline (creditRoundWin, below)
   * goes through exactly the same stale guard, round timeline, live push and win rule. Two ways in,
   * one rule: Sam's first-to-N is stated once, here, and a second source cannot quietly grow a
   * second interpretation of it.
   */
  function applyScoreline(match, score, limit, map) {
    if (!Number.isInteger(limit) || limit < 1 || ![1, 2].every(n => Number.isInteger(score[n]) && score[n] >= 0 && score[n] <= limit)
        || (score[1] === limit && score[2] === limit)) {
      return { ok: false, error: 'score outside agreed rules' };
    }
    noteScoreLimit(match, limit);
    // A score never goes DOWN. Reports can arrive out of order (they are independent HTTP calls
    // on a 15 s timer), and applying a stale one would un-win a won match.
    const known = match.score || { 1: 0, 2: 0 };
    if ((score[1] < known[1]) || (score[2] < known[2])) {
      return { ok: false, error: 'stale report', score: known };
    }
    // THE ROUND TIMELINE (2026-09-15). The score reports arrive every 15 s and only the LATEST
    // was kept, so a finished match remembered "7-3" and nothing about how it got there. That is
    // the one thing a match-detail screen cannot reconstruct afterwards, and it costs almost
    // nothing to keep: a round in Bodybomb is won by exactly one side, so every step in either
    // number IS a round, and which number moved says who took it.
    //
    // Derived from the score rather than from a round counter on purpose. GetCurrentRound() is
    // not in the report, and SpawnCount was measured to count spawns rather than rounds
    // (2026-09-15), so the score is the only thing here that means what it says.
    //
    // Reports can also repeat unchanged - the same 7-3 fifteen seconds later - which must not
    // become a second round.
    const before = match.score || { 1: 0, 2: 0 };
    const gained = [1, 2].filter((n) => score[n] > (before[n] || 0));
    if (gained.length) {
      if (!Array.isArray(match.rounds)) match.rounds = [];
      // One report can carry more than one round if a tick was lost in transit. Recording it as
      // a single round would make the strip shorter than the score, which is the kind of quiet
      // disagreement that makes a UI look broken; `steps` says how many are being attributed.
      for (const side of gained) {
        const steps = score[side] - (before[side] || 0);
        match.rounds.push({ at: Date.now(), won: side, steps,
                            1: score[1], 2: score[2] });
      }
      // A defensive cap. Bodybomb is 12 rounds and the limit is checked below, but a tampered
      // report must not be able to grow an unbounded array on a live match.
      if (match.rounds.length > ROUND_KEEP) match.rounds = match.rounds.slice(-ROUND_KEEP);
    }

    match.score = score;
    match.score_limit = limit;
    match.score_at = Date.now();
    if (map) match.team_map = map; // remembered, so a later -1 anchor still lands

    // The live score is not the stakes (M2 is about Elo, which nobody is told before the end),
    // and the players can see this on their own screens anyway. It is sent so the hub can show
    // the match it is sitting behind.
    for (const p of match.players) {
      sendTo(p.player_id, { type: 'match_score', match_id: match.id, score, limit });
    }

    // SAM'S RULE, and the whole point: first to the limit wins.
    const reached = [1, 2].filter((n) => score[n] >= limit);
    if (!reached.length) return { ok: true, score, limit, finished: false };
    const winner = reached[0];
    beginCollection(match, winner, score, limit);
    return { ok: true, score, limit, finished: true, winner, match_id: match.id,
             collecting: !!match.collecting };
  }

  /**
   * The score limit the GAME says it is playing to, remembered off whichever report carried it.
   *
   * The derived path has no limit of its own - a round-ended row says who won a round and nothing
   * about how many win a match - so it reads the last one the pak reported. In practice that is the
   * heartbeat, every 5 s, from the first beat of the match.
   */
  function noteScoreLimit(match, limit) {
    if (Number.isFinite(limit) && limit > 0) match.game_score_limit = Math.min(999, limit);
  }

  /**
   * A ROUND WAS WON BY AN IN-GAME TEAM. Count it, and settle the match if that was the last one.
   *
   * WHY THIS EXISTS (Sam, 2026-09-16/17, measured across four live 1v1s). BB5's native team score
   * lives in `ABodycamGameState::Teams`, and in a two-player test that array is EMPTY: every
   * ch_bb5_score_none in the probe ring reads `teams=0`, GetTeamData returns zeros, GetRoundWinTeam
   * is -1 and GetPlayerScore is -1. The cause is the DataAsset's `TeamConfig.MaxPlayers`, which the
   * game needs above 2 to build the array at all - and which is ALSO what the start gate reads as
   * `want`, so a 2-player match only ever starts when it is exactly 2. The two requirements
   * contradict each other, so there is no setting that gives a 1v1 both a team array and a start.
   * A real ten-player match has neither problem and reports its score natively; this is the path
   * that stops a two-machine test from being unsettleable.
   *
   * WHAT IT COUNTS. Not kills, and not the game's score: ROUNDS, attributed by who was still alive
   * when the round ended. Bodybomb rounds are elimination rounds, so the side that is not wiped took
   * it, and both the OnRoundEnded row and the kill feed carry both teams' alive counts at exactly
   * the moment that became true. Anything ambiguous - neither side wiped (a bomb round, a timeout),
   * or both (a last-man trade) - is NOT guessed at: it is skipped, and the match simply runs on. An
   * under-count leaves a match unsettled, which the timeout already handles; a wrong count hands
   * somebody a win they did not earn, which nothing handles.
   *
   * IDEMPOTENT BY ROUND NUMBER, because the same round arrives more than once: the delegate row and
   * the kill that ended it are two witnesses to one event, and a match with a delegate that fires
   * unreliably (measured: 2 rows for 3 rounds, then 3 for 3) needs both without double-counting.
   */
  function creditRoundWin(match, round, ingameTeam, why) {
    if (!match || match.finished || match.collecting) return null;
    if (!Number.isFinite(round) || round < 0 || round > 99) return null;
    if (!Number.isFinite(ingameTeam) || ingameTeam < 0) return null;

    match.round_wins = match.round_wins || {};      // round number -> in-game team that took it
    if (match.round_wins[round] !== undefined) return null;      // already witnessed

    // WHICH OF OUR SIDES THAT IS. `match.ingame` is the team sweep's map of steam id -> in-game
    // TeamID, and teamsAgree turns it into the side mapping the start gate already insisted on
    // before this match went live. `team_map`, learned from a native score report, is the fallback
    // for a match that got one before the array went empty.
    let side = null;
    const verdict = teamsAgree(match);
    if (verdict && verdict.agree && Array.isArray(verdict.mapping)) {
      const hit = verdict.mapping.find((m) => m.ingame === ingameTeam);
      if (hit) side = hit.side;
    }
    if (!side && match.team_map) side = match.team_map[ingameTeam];
    // No mapping yet is not an error and must not be recorded as one: the sweep may simply not have
    // reached everybody. Leaving the round uncredited lets a later round carry the match instead of
    // pinning a win on a team we cannot name.
    if (side !== 1 && side !== 2) return null;

    match.round_wins[round] = ingameTeam;
    // Freeze the hub team for display; game team mappings may change at halftime.
    (match.round_results ||= {})[round] = side;
    const score = { 1: 0, 2: 0 };
    for (const [n, team] of Object.entries(match.round_wins)) {
      void n;
      let s = null;
      if (verdict && verdict.agree && Array.isArray(verdict.mapping)) {
        const hit = verdict.mapping.find((m) => m.ingame === team);
        if (hit) s = hit.side;
      }
      if (!s && match.team_map) s = match.team_map[team];
      if (s === 1 || s === 2) score[s] += 1;
    }

    const forced = match.start_ready_verified ? match.expected_score_limit : forcedScoreLimit();
    const limit = match.start_ready_verified ? match.expected_score_limit : match.reportToken && match.legacyReportAuth !== true
      ? (match.agreedScoreLimit || DEFAULT_SCORE_LIMIT) : forced !== null ? forced
      : (Number.isFinite(match.game_score_limit) && match.game_score_limit > 0
          ? match.game_score_limit : DEFAULT_SCORE_LIMIT);
    const result = applyScoreline(match, score, limit, null);
    return { ...result, round, ingame_team: ingameTeam, side, derived: why || 'round' };
  }

  /**
   * The alive counts at the end of a round, turned into the team that took it.
   *
   * `null` unless EXACTLY ONE side is wiped, which is the only shape that means what we need it to
   * mean. See creditRoundWin on why an ambiguous round is skipped rather than guessed.
   */
  function winnerFromAlive(a0, a1) {
    if (!Number.isFinite(a0) || !Number.isFinite(a1)) return null;
    if (a0 > 0 && a1 === 0) return 0;
    if (a1 > 0 && a0 === 0) return 1;
    return null;
  }

  /**
   * The match is DECIDED. Hold it open for a moment so the final stats can land, then settle.
   *
   * Decided and settled are two different things and this is the seam between them. Nothing about
   * the result can change from here: the winner and the scoreline are fixed, further score
   * reports are ignored rather than applied. What is still moving is the STATS, which the sweep
   * delivers one player at a time - see COLLECT_SECONDS.
   *
   * Idempotent, because OnMatchEnded fires more than once: it was seen twice in the 2026-09-15
   * match, 46 s apart. The first call wins and every later one is a no-op.
   */
  function beginCollection(match, winner, score, limit) {
    if (match.finished || match.collecting) return;
    if (match.start_ready_verified) {
      match.collecting = { winner, score, limit, since: Date.now() };
      match.stats = match.stats || { players: [] };
      return;
    }
    if (collectFor <= 0) { finishMatch(match, winner, score, limit); return; }
    match.collecting = { winner, score, limit, since: Date.now(), deadline: Date.now() + collectFor * 1000 };
    match.stats = match.stats || { players: [] };
    match.stats.since = match.collecting.since;   // rows older than this do not count as final
    match.collectTimer = setTimeout(() => {
      match.collectTimer = null;
      finishMatch(match, winner, score, limit);
    }, collectFor * 1000);
    if (match.collectTimer.unref) match.collectTimer.unref();
  }

  /** Every player on the roster has reported since the match ended: nothing is left to wait for. */
  function collectionComplete(match) {
    if (!match.collecting || match.start_ready_verified) return false;
    if (match.combatState && !match.combatState.coverage.closed) return false;
    const since = match.collecting.since;
    const seen = (match.stats && match.stats.seenAt) || {};
    const rounds = Number(match.collecting.score[1]) + Number(match.collecting.score[2]);
    const seenRounds = (match.stats && match.stats.seenRounds) || {};
    return match.players.every((p) => Number(seen[p.player_id]) >= since && Number(seenRounds[p.player_id]) >= rounds);
  }

  /** Called whenever a stats row lands, to end the window early once there is nothing to wait for. */
  function maybeSettleEarly(match) {
    if (!match.collecting || match.finished) return false;
    if (!collectionComplete(match)) return false;
    if (match.collectTimer) { clearTimeout(match.collectTimer); match.collectTimer = null; }
    const { winner, score, limit } = match.collecting;
    finishMatch(match, winner, score, limit);
    return true;
  }

  // One player's line in a stats report:
  //   <steamId>|<kills>:<deaths>:<spawnCount>:<score>:<roundsWon>:<clutches>
  //
  // THE THIRD FIELD IS NOT ROUNDS PLAYED. It was read as that until 2026-09-15, when the first
  // real match (bots filling a solo lobby) reported `spawnCount` 4 for a match of 2 rounds, and
  // the two numbers sat side by side in the same payloads: round 1 -> 3, round 2 -> 4. It is
  // `SpawnCount`, which counts SPAWNS, warm-up respawns included, so it runs `rounds + warm-up`
  // and saturates the clamp for anyone who played throughout. As a denominator it silently
  // skewed `kpr` and, through `presence`, how much a match moved a rating at all.
  //
  // The real round count arrives in the SAME payload: the pak sends `GetCurrentRound()` as the
  // event's timestamp (bb5_graphs.gm_stat_report `st_rd`), which server.cjs hands over as
  // `rounds` and which is stored ONCE for the match. So no per-player `roundsPlayed` is recorded
  // at all: `playerMetrics` already falls back to the match's total, and in BB5 that IS every
  // player's total - one life per round, nobody joins mid-match.
  //
  // Stamping it per row instead was tried and is WRONG: the sweep visits one player every 3 s,
  // so a given player's last row can be half a minute stale, and the 2026-09-15 match settled
  // with the host's last row still reading round 1 of 2 - halving his presence and doubling his
  // kills-per-round. The match's count is never stale; a player's copy of it is.
  //
  // The trailing fields are optional and the pak currently stops after `score` - the last two
  // need per-round accumulation it cannot do yet (docs/valuation.md). Absent is NOT zero: the
  // valuation drops the component and redistributes its weight, which is why the shape is
  // positional-with-a-tail rather than something that has to guess.
  //
  // `score` is the game's own APlayerState score, the figure its scoreboard sorts on
  // (SortPlayersByScore).
  //
  // IT CAN BE NEGATIVE, and that is not hypothetical: the first live sweep (2026-09-15) reported
  // `76561198000000001|0:0:1:-1` before any round had started. `GetPlayerScore` answers **-1**,
  // not 0, for a player who has not scored. The first version of this pattern accepted only
  // digits there, so it matched nothing and every single row was thrown away as 'no usable
  // rows' - a whole feature silently dead because of one missing `-`.
  //
  // So the score accepts a sign. Nothing else does: kills, deaths and rounds cannot be negative
  // and a minus in one of those is a corrupt report, not a sentinel.
  //
  // AND THE TAIL IS SPLIT, NOT PATTERN-MATCHED. The first version made each trailing field its
  // own optional group, which is ambiguous the moment one of them can be negative: `0:0:-1:0`
  // matched by SKIPPING roundsPlayed and sliding the -1 into the score slot, so a corrupt row
  // parsed as a valid one with everything shifted a place. Splitting on ':' and reading by
  // position cannot do that - a field is the field it is in.
  const STAT_ROW = /^(\d{17})\|(-?\d{1,6}(?::-?\d{1,6}){1,5})$/;

  // ---- THE KEYED ROW (2026-09-15) -------------------------------------------------------------
  //
  // `<steamId>|k=3;d=1;t=0;s=250;a=1;b=0;p=34`
  //
  // SEPARATED BY ';' AND NOT ',' - deliberately, and it cost a debugging round to learn why. The
  // `rows` field is split on ',' so one call can carry several players, so a comma inside a row is
  // eaten by that split: the row was torn into fragments, only the first kept its SteamID, and
  // exactly one field survived. No error anywhere - it recorded `kills` and silently dropped
  // everything else. ',' separates PLAYERS, ';' separates that player's FIELDS.
  //
  // Positional was a compromise for a wire we believed was tiny. It is not: a field was measured
  // to carry 2048 characters intact, which is fifty times what any row needs. So the constraint
  // that forced positional is gone, and positional has cost us twice - a negative score matched
  // nothing and binned every row, and an all-optional tail let `0:0:-1:0` parse by SKIPPING a
  // field and sliding the -1 into the next slot. Both bugs are impossible in a keyed row: a field
  // is named, so it cannot move, and absent is unambiguously absent rather than "shifted".
  //
  // It is also forward-compatible in the direction that matters. An unknown key is IGNORED, so the
  // pak can start sending something new before the server knows what it is, instead of the two
  // having to change together - which for a pak means a cook, a repack and a reinstall.
  //
  // Both shapes are accepted, and will be: the keyed row is detected by the '=' and the positional
  // parser is untouched, so a pak in the field keeps working exactly as it does today.
  const STAT_KEYS = {
    // KILLS ARE A NET SCORE AND CAN BE NEGATIVE. Measured 2026-09-15: Sam killed one enemy and two
    // team-mates in round 1 and `Kill` read **-1**. A team kill DECREMENTS it. Treating it as a
    // count that cannot be negative threw the whole row away as corrupt - every sample of that
    // round was dropped, which is the third time a sign has silently binned real data on this
    // channel. It is signed here and nowhere is it assumed to be a count.
    k: { key: 'kills', signed: true },
    d: { key: 'deaths' },
    sp: { key: 'spawnCount' },                  // SPAWNS, not rounds - never a denominator
    // NOT A PLAYER SCORE. Measured 2026-09-15: it tracked the player's TEAM round wins exactly -
    // 0 while the score was 0:0, 1 at 0:1, 2 at 0:2, changing in step with the scoreline and never
    // otherwise. It is the same number for all five players on a side, so it discriminates nobody,
    // and feeding it to a performance component just counts the match outcome a second time.
    // Ingested under its own name so it cannot be mistaken for one; `spr` gets nothing until a
    // real per-player score exists. Still -1 until the player has spawned.
    s: { key: 'teamScore', sentinelBelow: 0 },
    t: { key: 'teamId', sentinelBelow: 0 },     // -1 for the first ~30 s of the match world
    a: { key: 'alive', bool: true },            // raw material for clutch detection
    b: { key: 'isBot', bool: true },            // said outright, instead of inferred from a blank id
    l: { key: 'lateJoin', bool: true },         // must not be rated like someone who played from round 1
    x: { key: 'spectator', bool: true },
    p: { key: 'ping' },                         // also the missing term in matchmaker.toleranceFor
    w: { key: 'roundsWon' },
    c: { key: 'clutches' },
    pt: { key: 'partyId', text: true },         // the GAME's own party grouping, independent of our hub's
  };
  const KEYED_ROW = /^(\d{17})\|([A-Za-z]{1,3}=[^,;]{0,32}(?:;[A-Za-z]{1,3}=[^,;]{0,32})*)$/;

  /** One keyed tail -> the same row shape the positional parser produces, or null if corrupt. */
  function parseKeyed(steamId, tail) {
    const row = { steamId };
    for (const pair of tail.split(';')) {
      const at = pair.indexOf('=');
      if (at < 1) continue;
      const spec = STAT_KEYS[pair.slice(0, at)];
      if (!spec) continue;                       // unknown key: ignored, never fatal
      const raw = pair.slice(at + 1);
      if (raw === '') continue;                  // present but empty is NOT reported
      if (spec.text) { row[spec.key] = raw.slice(0, 32); continue; }
      // Booleans arrive as Conv_BoolToString's "true"/"false", not as 1/0 - Number("true") is NaN,
      // which would have dropped every flag in silence. Digits are accepted too, so a future pak
      // that sends 1/0 needs no server change.
      if (spec.bool) {
        const lower = raw.toLowerCase();
        if (lower === 'true') { row[spec.key] = true; continue; }
        if (lower === 'false') { row[spec.key] = false; continue; }
        const flag = Number(raw);
        if (Number.isFinite(flag)) row[spec.key] = flag !== 0;
        continue;
      }
      const value = Number(raw);
      if (!Number.isFinite(value)) continue;
      // A sentinel means "not known yet" and is dropped, exactly as the positional parser does
      // with a -1 score. Anything else that cannot be negative and is means a corrupt row.
      if (value < 0 && !spec.signed) {
        if (spec.sentinelBelow !== undefined) continue;   // a sentinel: not known yet
        return null;                                      // a count that cannot be negative, and is
      }
      row[spec.key] = value;
    }
    return row;
  }
  // Which position is which, and whether it is allowed to be below zero.
  const STAT_FIELDS = [
    { key: 'kills', signed: false },
    { key: 'deaths', signed: false },
    { key: 'spawnCount', signed: false },   // SPAWNS, not rounds - see above; never a denominator
    { key: 'score', signed: true },        // -1 until the player has spawned; see above
    { key: 'roundsWon', signed: false },
    { key: 'clutches', signed: false },
  ];

  /**
   * PER-PLAYER STATS from the host's game (tier 1 - docs/match-data.md).
   *
   * Accepts either shape, because which one the pak can actually send is still open:
   *   * one call carrying ONE player      -> `rows` is a single line
   *   * one call carrying the WHOLE roster -> `rows` is lines joined by ','
   * The second needs a field long enough to hold ~280 characters, and that has NOT been
   * measured - nothing the game has sent so far is over 30. The first needs a latent
   * SendAttributionEvent inside a ForEach, which bb5_graphs.py already warns is trouble.
   * Supporting both costs one `split`, and means whichever way that question falls, this side
   * is already right.
   *
   * Accumulated onto the LIVE match and only read at settle time, so a report that arrives
   * late, twice, or never changes nothing except how much the valuation knows. Last write wins
   * per player: the pak sends running totals, so the newest is the truest.
   */
  function gameReportedStats(steamId, fields) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return { ok: false, error: 'not a steamid64' };
    const match = matches.get(inMatch.get(id));
    if (!match) return { ok: false, error: 'no match' };
    if (match.state !== 'live' && match.state !== 'connecting') {
      return { ok: false, error: `match is ${match.state}` };
    }
    if (String(match.host || '') !== id) return { ok: false, error: 'not the host' };
    if (echoMismatch(fields, match)) return { ok: false, error: 'wrong match id' };

    if (match.final_snapshot) return { ok: true, ignored: true };
    const roster = new Set(match.players.map((p) => p.player_id));
    match.stats = match.stats || { players: [] };
    const rounds = Number(String((fields && fields.rounds) || '').trim());
    // A HIGH-WATER MARK, so it can never go backwards. Reports are independent HTTP calls and do
    // arrive out of order; a late one stamped round 1 turning up during round 3 used to overwrite
    // this outright. That matters more than it looks: `stats.rounds` is the round total the
    // valuation divides by, so a stale report could halve everyone's denominator - doubling their
    // kills per round and cutting their presence - on a number nobody would think to check.
    // Found by the per-round series tests, not by anything that went wrong in a match.
    if (Number.isFinite(rounds) && rounds > 0) {
      match.stats.rounds = Math.min(99, Math.max(Number(match.stats.rounds) || 0, rounds));
    }
    if (match.score_limit) match.stats.limit = match.score_limit;

    const lines = String((fields && fields.rows) || '')
      .split(',').map((s) => s.trim()).filter(Boolean);
    const taken = [];
    for (const line of lines.slice(0, 10)) {
      // A keyed row is recognised by its '=' and parsed by name; anything else goes down the
      // positional path unchanged, so a pak already in the field keeps working.
      const keyed = line.includes('=') ? KEYED_ROW.exec(line) : null;
      const m = keyed || STAT_ROW.exec(line);
      if (!m) continue;
      // A steamId that was not in this match is DROPPED, not recorded - the same rule
      // adoptLobby uses, and for the same reason: the roster is not the host's to edit.
      const playerId = identity.playerFor(match, m[1]);
      if (!roster.has(playerId)) continue;
      // Read the tail by position. A field that may not be negative and is, means the report is
      // corrupt rather than early - drop the whole row rather than guess which field slipped.
      //
      // A NEGATIVE SCORE, though, IS A SENTINEL and not a score. Watched live on 2026-09-15:
      // `GetPlayerScore` answered -1 for the first half-minute of the match world and turned
      // into 0 once the player had properly spawned. Recording -1 as a real value would rank
      // that player dead last on `spr` for no reason but timing, so it is dropped and the
      // valuation simply has no score for them until a real one arrives.
      let row;
      if (keyed) {
        row = parseKeyed(m[1], m[2]);
        if (!row) continue;                    // a count that cannot be negative, and is
      } else {
        const parts = m[2].split(':').map(Number);
        row = { steamId: m[1] };
        let corrupt = false;
        STAT_FIELDS.forEach((field, i) => {
          const value = parts[i];
          if (value === undefined || !Number.isFinite(value)) return;      // not reported
          if (value < 0) {
            if (!field.signed) corrupt = true;   // a count that cannot be negative, and is
            return;                              // a signed sentinel: leave it unreported
          }
          row[field.key] = value;
        });
        if (corrupt) continue;
      }
      // A bot that says so is dropped here rather than relying on its id being blank. The pak
      // skips them at the source, but an older pak does not, and a bot must never reach a ladder.
      if (row.isBot) continue;
      if(m[1]!==playerId)row.gameSteamId = m[1];
      row.steamId = playerId;
      // Keep older rounds as historical evidence, but never let their delayed
      // arrival replace the current totals or satisfy final-stat collection.
      const samples = match.stats.series && match.stats.series[row.steamId];
      const latestRound = samples ? Math.max(-1, ...Object.keys(samples).map(Number).filter(Number.isFinite)) : -1;
      if (Number.isFinite(rounds) && rounds < latestRound) {
        samples[rounds] = row;
        taken.push(row.steamId);
        continue;
      }
      const at = match.stats.players.findIndex((p) => p.steamId === row.steamId);
      if (at >= 0) match.stats.players[at] = row;
      else match.stats.players.push(row);
      // WHEN this player was last heard from, kept beside the rows rather than on them so the
      // row stays exactly the shape the valuation reads. It is what closes the collection window
      // early: once every player has reported since the match ended, there is nothing to wait for.
      match.stats.seenAt = match.stats.seenAt || {};
      match.stats.seenAt[row.steamId] = Date.now();
      match.stats.seenRounds = match.stats.seenRounds || {};
      match.stats.seenRounds[row.steamId] = rounds;

      // THE TEAM-KILL FLOOR IS GONE (Sam, 2026-09-15: "net score/kills is a useless metric").
      //
      // It counted falls in the net kill counter as proof of a team kill, which was the best that
      // could be done while the counter was all we had. The kill feed makes it pointless: a team
      // kill is now a named event, counted exactly, and on the real match the feed returned six
      // where the floor had found two. Keeping a worse estimate beside a better one only invites
      // somebody to use it.

      // LEARN THE TEAM MAPPING FROM THE HOST'S OWN ROW, as soon as it knows its team.
      //
      // `team_map` turns the game's team ids into ours, and it used to be learned only from a score
      // report - which is on a 15 s timer, so it did not exist for most of the first round.
      // Replaying the real match, that cost four of six team kills: they were classified before
      // anything had said which game team was which.
      //
      // The host's stat row carries its own TeamID and the server already knows which side it put
      // the host on, so one row settles the whole mapping. There are exactly two teams, so naming
      // one names the other.
      if (!match.team_map && row.steamId === match.host
          && Number.isFinite(row.teamId) && row.teamId >= 0) {
        const hostSide = teamOf(match, match.host);
        if (hostSide) {
          const otherGame = row.teamId === 0 ? 1 : 0;
          match.team_map = { [row.teamId]: hostSide, [otherGame]: hostSide === 1 ? 2 : 1 };
        }
      }

      // THE PER-ROUND SERIES (layer 1b, docs/round-context.md section 3).
      //
      // The sweep already stamps every row with GetCurrentRound(), and until now the server threw
      // that away: a player's row was overwritten on each arrival, so the six-or-so samples taken
      // during a round collapsed into one number and every per-round question became unanswerable.
      //
      // Keeping them keyed by (steamId, round) costs nothing and turns the stream we ALREADY send
      // into a per-round history. Within a round the last sample still wins, which is what we
      // want: the latest sample stamped round N is the one closest to the end of round N, so
      // differencing consecutive rounds gives what a player actually did in each one.
      //
      // `players` is left exactly as it was - running totals, last write wins - because that is
      // what the valuation reads today and this must not change any existing number.
      // Filed under THIS REPORT's round, not `stats.rounds`. Those are different numbers:
      // `stats.rounds` is the match's high-water mark, so a report that arrives late - stamped
      // round 1 while the match has reached round 3 - would be filed under 3 and corrupt two
      // rounds at once. And round 0 is kept: it is the warm-up baseline every delta counts from,
      // and `stats.rounds` never holds it, because it only records rounds above zero.
      if (Number.isFinite(rounds) && rounds >= 0 && rounds <= 99) {
        match.stats.series = match.stats.series || {};
        const mine = match.stats.series[row.steamId] || (match.stats.series[row.steamId] = {});
        mine[rounds] = row;
      }
      taken.push(row.steamId);
    }
    if (!taken.length) return { ok: false, error: 'no usable rows' };
    const settled = maybeSettleEarly(match);
    return { ok: true, players: taken.length, known: match.stats.players.length,
             rounds: match.stats.rounds || null,
             collecting: !!match.collecting, settled };
  }

  // ---- THE ROUND SNAPSHOT --------------------------------------------------------------------
  //
  //   n=<round>;w=<winningTeam>;sec=<length>;a0=<alive team 0>;a1=<alive team 1>
  //
  // One call per round, from OnRoundEnded. Keyed for the same reasons the stat row is, and with an
  // unknown key ignored the same way, so the pak can add to it without waiting for a deploy.
  const ROUND_KEYS = {
    n: 'round', w: 'winTeam', sec: 'seconds', a0: 'alive0', a1: 'alive1', obj: 'objectiveTeam',
  };

  /**
   * WHAT A ROUND WAS (layer 1 - docs/round-context.md).
   *
   * The thing a match total can never reconstruct: which rounds were won, how long they took and
   * how many were left standing. `roundWinShare` and the round's own decisiveness are built from
   * this, and both have sat in the valuation's component table unfed since it was written.
   *
   * Accumulated onto the live match and read at settle time, like the stats - so a report that
   * arrives late, twice or never changes nothing but how much the valuation knows. Keyed by round
   * number, so a repeat is idempotent rather than a duplicate.
   */
  function gameReportedRound(steamId, fields) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return { ok: false, error: 'not a steamid64' };
    const match = matches.get(inMatch.get(id));
    if (!match) return { ok: false, error: 'no match' };
    if (match.final_snapshot) return { ok: true, ignored: true };
    if (match.state !== 'live' && match.state !== 'connecting') {
      return { ok: false, error: `match is ${match.state}` };
    }
    if (String(match.host || '') !== id) return { ok: false, error: 'not the host' };
    if (echoMismatch(fields, match)) return { ok: false, error: 'wrong match id' };

    const raw = String((fields && fields.row) || '').trim();
    if (!raw) return { ok: false, error: 'empty' };
    const out = {};
    for (const pair of raw.split(';')) {
      const at = pair.indexOf('=');
      if (at < 1) continue;
      const key = ROUND_KEYS[pair.slice(0, at)];
      if (!key) continue;                       // unknown key: ignored, never fatal
      const value = Number(pair.slice(at + 1));
      if (Number.isFinite(value)) out[key] = value;
    }
    // A round with no number cannot be filed, and -1 is the game's "nobody won yet" rather than a
    // team id - the same sentinel rule the anchor and the score use.
    if (!Number.isFinite(out.round) || out.round < 0 || out.round > 99) {
      return { ok: false, error: 'no round number' };
    }
    if (out.winTeam !== undefined && out.winTeam < 0) delete out.winTeam;

    // WHERE IT CAME FROM, kept because it is the open question this design routes around.
    // The delegate is exact; the heartbeat is sampled. A delegate row is never overwritten by a
    // heartbeat one - it was taken AT the round's end, and a later sample cannot improve on that -
    // but a heartbeat row is replaced freely by a newer one, so the value kept for a round is the
    // last sample taken during it.
    out.source = String((fields && fields.source) || 'delegate');
    // The alive counts are the baseline the kill feed differences against, and the round's FIRST
    // kill has no earlier kill to compare with - which is exactly the kill that was missed on the
    // real match, twice, once per round. The heartbeat carries them, so it supplies that baseline.
    if (Number.isFinite(out.alive0) && Number.isFinite(out.alive1)) {
      match.lastAlive = { round: out.round, a0: out.alive0, a1: out.alive1 };
    }
    match.rounds = match.rounds || {};
    const had = match.rounds[out.round];
    // Display snapshots are independent of the legacy score-step container.
    const candidate = had?.source === 'delegate' && out.source !== 'delegate' ? had : out;
    const displayReport = (match.round_reports ||= {})[out.round];
    if (!displayReport || displayReport.source !== 'delegate' || candidate.source === 'delegate') {
      match.round_reports[out.round] = { ...candidate };
      if (candidate.source === 'delegate' && Number.isInteger(candidate.winTeam)) {
        const mapping = teamsAgree(match);
        const side = (mapping?.agree ? mapping.mapping?.find(m => m.ingame === candidate.winTeam)?.side : null)
          || match.team_map?.[candidate.winTeam];
        if (side === 1 || side === 2) (match.round_results ||= {})[out.round] ??= side;
      }
    }
    if (had && had.source === 'delegate' && out.source !== 'delegate') {
      return { ok: true, round: out.round, known: Object.keys(match.rounds).length,
               row: had, kept: 'delegate' };
    }
    match.rounds[out.round] = out;
    // ...and the round is a SCORE as well as a snapshot. OnRoundEnded fires at the instant the
    // round ends, so its alive counts are the exact ones - a heartbeat row is a sample and may have
    // been taken after the next round's respawn, which is why only a delegate row scores here. The
    // kill feed catches the rounds this delegate misses; both are idempotent per round number.
    const derived = out.source === 'delegate'
      ? creditRoundWin(match, out.round, winnerFromAlive(out.alive0, out.alive1), 'round-ended')
      : null;
    return { ok: true, round: out.round, known: Object.keys(match.rounds).length, row: out,
             ...(derived ? { scored: derived } : {}) };
  }

  // ---- THE KILL FEED -------------------------------------------------------------------------
  //
  //   k=<killerSteamId>;v=<victimSteamId>;n=<round>;t=<secondsIntoRound>;a0=<alive>;a1=<alive>
  //
  // Sam, 2026-09-15: *"almost like a kill feed of which user killed which user and based off the
  // hub team selection it can then tell if a teammate killed a teammate"*. That is the design, and
  // the second half of it is the part that matters.
  //
  // THE TEAMS ARE OURS, NOT THE GAME'S. The server assigned them when it formed the match, so
  // `match.teams` is authoritative and a team kill is a lookup rather than an inference. It needs
  // no TeamID from the host at all - which is better than trusting one, since everything the host
  // sends is its unauthenticated word, and a host that wanted to hide its team killing could
  // simply report a different TeamID. It cannot rewrite who we put on which side.
  //
  // And unlike the kill COUNTER, this cannot alias: a team kill and an enemy kill are two events,
  // not a quantity that nets to zero. That is the whole reason this exists (see section 4b of
  // docs/round-context.md).
  //
    // A NULL KILLER IS EXPECTED, not an error - fall damage, the bomb, and any world kill produce a
  // victim with no instigator. The field simply arrives empty and the death is recorded as one
  // nobody is credited with.
  const KILL_KEYS = { k: 'killer', v: 'victim', n: 'round', t: 'at', a0: 'alive0', a1: 'alive1' };

  /**
   * ONE KILL (layer 2 - docs/round-context.md).
   *
   * Appended to a timeline on the live match and read at settle time. Ordered by arrival, which is
   * the order they happened: they are separate events from one machine, and a burst of them was
   * measured to arrive intact and in sequence.
   */
  function gameReportedKill(steamId, fields) {
    const id = String(steamId || '');
    if (!identity.validPlayer(id)) return { ok: false, error: 'not a steamid64' };
    const match = matches.get(inMatch.get(id));
    if (!match) return { ok: false, error: 'no match' };
    if (match.final_snapshot) return { ok: true, ignored: true };
    if (match.state !== 'live' && match.state !== 'connecting') {
      return { ok: false, error: `match is ${match.state}` };
    }
    if (String(match.host || '') !== id) return { ok: false, error: 'not the host' };
    if (echoMismatch(fields, match)) return { ok: false, error: 'wrong match id' };

    const out = {};
    for (const pair of String((fields && fields.row) || '').split(';')) {
      const at = pair.indexOf('=');
      if (at < 1) continue;
      const key = KILL_KEYS[pair.slice(0, at)];
      if (!key) continue;
      const raw = pair.slice(at + 1).trim();
      if (raw === '') continue;
      if (key === 'killer' || key === 'victim') {
        if (identity.validSteam(raw)) out[key] = raw;      // anything else is not an account
      } else {
        const value = Number(raw);
        if (Number.isFinite(value) && value >= 0) out[key] = value;
      }
    }
    for (const key of ['killer', 'victim']) if (out[key]) {
      const player = identity.playerFor(match, out[key]);
      if (!player) return {ok:false,error:key+' not on the roster'};
      if(out[key]!==player)out[key + 'GameSteamId'] = out[key];
      out[key] = player;
    }
    // Without a victim there is no death to record. A missing KILLER is fine - that is a world
    // kill - and a victim we never put in this match is not the host's to add.
    // A victim we cannot NAME is still a death worth recording, as long as somebody is credited
    // with it: bots have no SteamID, so in a bot match no victim can ever be named, and refusing
    // those threw away every kill in the match that proved this feature works.
    if (!out.victim && !out.killer) return { ok: false, error: 'neither killer nor victim' };
    let victimTeam = 0;
    if (out.victim) {
      victimTeam = teamOf(match, out.victim);
      if (!victimTeam) return { ok: false, error: 'victim not on the roster' };
      out.victimTeam = victimTeam;
    }
    if (out.killer) {
      const killerTeam = teamOf(match, out.killer);
      if (!killerTeam) return { ok: false, error: 'killer not on the roster' };
      out.killerTeam = killerTeam;
      out.teamKill = !!victimTeam && killerTeam === victimTeam && out.killer !== out.victim;
      out.suicide = !!out.victim && out.killer === out.victim;
    } else {
      out.teamKill = false;      // nobody did it, so nobody is blamed for it
      out.world = true;
    }

    // A named replay has a stable identity even after restoring the match. Do
    // this before changing the alive baseline or crediting any feed-derived stat.
    if (out.victim && Number.isFinite(out.round) && Number.isFinite(out.at)) {
      const keyOf = k => JSON.stringify([k.killer || null, k.victim, k.round, k.at, k.alive0 ?? null, k.alive1 ?? null]);
      const key = keyOf(out);
      const priorKill = (match.kills || []).find(k => keyOf(k) === key);
      if (priorKill) return { ok: true, duplicate: true, kills: match.kills.length, kill: priorKill };
    }

    // WHOSE SIDE DIED, FROM THE ALIVE COUNTS. Measured 2026-09-15: in a bot match every victim's
    // SteamID is empty, because bots have none - so the victim often cannot be named at all. The
    // alive counts still say which side lost somebody, and that is all a team kill needs: our own
    // roster says which side the KILLER is on, and `team_map` (learned from the score report's
    // anchor) turns the game's team ids into ours.
    //
    // Replayed against the real match this reconstructed all eighteen kills and agreed with Sam's
    // own count exactly - six team kills and eight enemies - once the heartbeat supplied the
    // round-start baseline.
    const prior = match.lastAlive;
    if (Number.isFinite(out.alive0) && Number.isFinite(out.alive1) && prior
        && prior.round === out.round) {
      const dropped = prior.a0 > out.alive0 ? 0 : (prior.a1 > out.alive1 ? 1 : null);
      const other = prior.a0 > out.alive0 ? prior.a1 > out.alive1 : false;
      if (dropped !== null && !other) {
        out.victimGameTeam = dropped;
        const mapped = match.team_map && match.team_map[dropped];
        if (mapped) out.victimTeamFromAlive = mapped;
      }
    }
    if (Number.isFinite(out.alive0) && Number.isFinite(out.alive1)) {
      match.lastAlive = { round: out.round, a0: out.alive0, a1: out.alive1 };
    }
    // A named victim always wins; the alive counts only answer for one we could not name.
    if (!out.victim && out.killer && out.victimTeamFromAlive) {
      out.victimTeam = out.victimTeamFromAlive;
      out.teamKill = out.killerTeam === out.victimTeam;
      out.inferred = true;      // said out loud: this is arithmetic, not two named accounts
    }

    match.kills = match.kills || [];
    if (match.kills.length >= 400) return { ok: false, error: 'too many kills' };
    match.kills.push(out);

    // THE FEED DRIVES THE PUNISHMENT (Sam, 2026-09-15: "the current implementation we just
    // discovered is the one i want to go with... i still want to hold the punishments accordingly
    // ... but using the feed is the most accurate way to get how many teamkills there were").
    //
    // The enforcement - the flags, the accident-versus-malice ruling, the elo and queue penalty -
    // is kept exactly as it is. Only its INPUT changes. It used to come from a second pak binding
    // on the same delegate that decided for itself what a team kill was; it now comes from the
    // feed, where the classification is done against the roster the SERVER assigned. On the match
    // that settled this the feed returned six team kills where the netted kill counter showed two.
    //
    // Only a kill with both ends NAMED is referred: teamKillReported rules on people, and an
    // inference from alive counts is not a person. In a real match every player has a SteamID, so
    // the only kills this skips are bot ones, which were never punishable anyway.
    let verdict = null;
    if (out.teamKill && out.killer && out.victim) {
      verdict = teamKillReported(match.host, {
        killer: out.killer, victim: out.victim,
        team: out.victimGameTeam, elapsed: out.at, round: out.round,
        alive0: out.alive0, alive1: out.alive1,
      });
      if (verdict && verdict.ok) out.verdict = verdict;
    }
    // ...AND THE KILL THAT ENDED THE ROUND IS A SCORE. A Bodybomb round ends when a side is wiped,
    // so the kill that takes one to zero alive names the round's winner at the exact instant it
    // becomes true - the same fact OnRoundEnded carries, from a delegate that has been measured to
    // miss rounds (2 rows for 3 rounds on 2026-09-16). Both feed one idempotent counter, so the
    // two witnesses to one round cannot count it twice, and either one alone is enough.
    const scored = creditRoundWin(match, out.round, winnerFromAlive(out.alive0, out.alive1), 'kill');
    return { ok: true, kills: match.kills.length, kill: out, verdict,
             ...(scored ? { scored } : {}) };
  }

  const resultReceipts = new Map();
  const resultSaves = new Map();
  const resultKey = id => `${prefix || 'hub:'}result:${id}`;

  async function readReceipt(id) {
    if (resultReceipts.has(id)) return resultReceipts.get(id);
    if (!store) return null;
    const raw = await store(['GET', settlementKey(id)], { strict: true });
    if (!raw) return null;
    const receipt = await combatStorage.unpack(store, typeof raw === 'string' ? identity.parse(raw) : identity.hydrate(structuredClone(raw)), settlementKey(id));
    if (receipt.match_id !== id || receipt.data_collected !== true) throw Error('invalid receipt');
    resultReceipts.set(id, receipt);
    return receipt;
  }

  function completionEvent(receipt, steamId) {
    const event = receipt.events[steamId];
    const rounds = receipt.full?.round_details || receipt.publicMatch?.round_details;
    // Round boards are shared by every participant. Persist them with the match
    // and attach them at delivery, including replay after a server restart.
    return event && !Array.isArray(event.round_details) && Array.isArray(rounds)
      ? { ...event, round_details: rounds } : event;
  }

  async function deliveredCompletionEvent(receipt, steamId) {
    try {
      const original=completionEvent(receipt,steamId);
      if(!original||original.voided)return original||null;
      const [event]=await restitution.annotate([{...original,id:receipt.match_id}]);
      if(!event.cheater_reverted)return original;
      const current=store?await readRating(steamId):ratingOf(steamId);
      const rank=progressLib.publicProgress(current,{top:isReaper(steamId,current.progress)});
      const movement={...rank,matches:current.matches,wins:current.wins,losses:current.losses,delta:0,rr_delta:0,arrows:0,placed:false,bdr_delta:rank.bdr===null?null:0,bdr_before:rank.bdr};
      const cheaters=new Set(event.cheaters||[]);
      return {...event,...movement,you:{...event.you,...movement},scoreboard:(event.scoreboard||[]).map(p=>({...p,cheater:cheaters.has(p.player_id),delta:0,rr_delta:0}))};
    } catch { return null; } // The durable receipt still authorizes cleanup when the optional display is unavailable.
  }

  async function completion(steamId, matchId) {
    const id = String(matchId || '');
    const pending = { ok: false, match_id: id, data_collected: false, close_allowed: false };
    if (!/^[0-9a-f]{16}$/.test(id)) return pending;
    try {
      const receipt = await readReceipt(id);
      if (!receipt || !receipt.participants.includes(steamId)) return pending;
      return { ok: true, match_id: id, data_collected: true, close_allowed: true,
        collected_at: receipt.collected_at, close_after: receipt.close_after, result: await deliveredCompletionEvent(receipt, steamId) };
    } catch { return pending; }
  }

  async function acceptCommitted(match, receipt) {
    resultReceipts.set(receipt.match_id, receipt);
    archived.set(receipt.match_id, receipt.full);
    // A lost-response retry can arrive after another match. Its receipt proves
    // completion; it must not roll newer career/history caches back in time.
    if (!match) return;
    for (const [id, record] of Object.entries(receipt.ratings)) {
      if ((record.revision || 0) >= (ratings.get(id)?.revision || 0)) ratings.set(id, record);
      ratingLoaded.add(id);
    }
    for (const [id, record] of Object.entries(receipt.careers || {})) {
      if ((record.result_revision || 0) >= (careers.get(id)?.result_revision || 0)) careers.set(id, record);
      frozenCareers.delete(id);
    }
    for(const [id,penalty] of Object.entries(receipt.penalties||{})){
      if((penalty.last||0)>=(penalties.get(id)?.last||0)){
        penalties.set(id,penalty);penaltyLoaded.add(id);
        sendTo(id,{type:'penalty',...penalty,seconds:Math.max(0,Math.ceil((penalty.until-Date.now())/1000)),rr:0,next_seconds:rungSeconds(penalty.count+1)});
      }
    }
    for (const [id, row] of Object.entries(receipt.history)) {
      history.set(id, [row, ...(history.get(id) || []).filter(r => r.id !== receipt.match_id)].slice(0, HISTORY_KEEP));
    }
    if (match) {
      clearTimeout(match.timer); clearTimeout(match.collectTimer);
      match.finished = { at: receipt.collected_at, winner: receipt.full.won_team, score: receipt.full.score };
      match.state = 'over';
      matches.delete(match.id); persisted.delete(match.id);
      for (const id of receipt.participants) {
        if (!inMatch.has(id) || inMatch.get(id) === match.id) frozenCareers.delete(id);
        if (inMatch.get(id) === match.id) inMatch.delete(id);
      }
      match.credited = true;
      await Promise.all(Object.keys(receipt.events).map(async id => {
        const event=await deliveredCompletionEvent(receipt,id);
        if(event)sendTo(id,event);
        sendTo(id, { type: 'match_over', match_id: match.id, score: receipt.full.score,
          data_collected: true, close_allowed: true });
      }));
      broadcast(stats()); drainQueue();
    }
  }

  async function commitPlayedResult(match, {ids,side,winner,draw,score,total,limit,host,id,mergedRows}) {
    const s0=score?.[1]||0,s1=score?.[2]||0;
      const save = withRatingLocks(ids, async () => {
        if (!store) throw Error('durable result store unavailable');
        await Promise.all([matchWriting.get(id), ...ids.flatMap(sid => [...(careerWrites.get(sid) || [])]), ...ids.map(drainRatingWrites),...ids.map(drainPenaltyWrites)]);
        await Promise.all(ids.map(async sid => {
          await readRating(sid, false);
          const raw = await store(['HGET', rosterKey(), sid], { strict: true });
          if (raw) careers.set(sid, { ...blankCareer(sid), ...identity.parse(raw) });
        }));
        const settled = winner ? settleMatch(match, winner, { calculate: true }) : ids.map(steamId => {
          const after = ratingOf(steamId), rank = progressLib.publicProgress(after);
          return { steamId, before: after, after, won: null, rr: { delta: 0 }, event: { type: 'match_result', match_id: id,
            won: null, draw: true, delta: 0, rr_delta: 0, ...rank, score, map: match.map,
            scoreboard: scoreboardOf(match), round_details: roundDetails(match), round_index_base: 0, rounds_played: total,
            you: { ...rank, rr_delta: 0 } } };
        });
        if (winner) {
          for (const row of settled) row.after.revision = (row.before.revision || 0) + 1;
          publishSettlement(match, winner, settled, Math.abs(s0 - s1) / Math.max(s0, s1, 1), true);
        }
        const now = Date.now();
        const reviewPlayers=duel?settled.filter(r=>!r.won&&require('./duel-matching.cjs').review(recentDuels.get(r.steamId),ids.find(id=>id!==r.steamId),now)).map(r=>r.steamId):[];
        const record = { ended: match.final_ended_at, outcome: 'played', reason:match.terminal?.reason||'', host, map: match.map, sides: match.sides || {} };
        const full = fullRecord(match, record);
        Object.assign(full, { terminal:match.terminal||null, won_team: winner || null, draw, score, final_stats: mergedRows, combat_end: match.combat_end, data_collected: true });
        const canonical = { repeat_review:reviewPlayers, terminal:match.terminal||null, version: settlementLib.VERSION, matchId: id,host_epoch:match.host_epoch||0, at: now, winner, draw, score, limit,
          report_digest: /^[a-f0-9]{64}$/.test(match.reportToken || '') ? crypto.createHash('sha256').update(match.reportToken).digest('hex') : null,
          mode:mode.id, analytics_context: analyticsContext(match),
          rules: Object.fromEntries(Object.entries(process.env).filter(([key]) => /^COMP_(RR_|PERF_|RANK_|RATING_|LEVEL_THRESHOLDS$|ARROW_|PLACEMENT_|REAPER_|W_|WEIGHT_|DECISIVE_|INT_|PRESENCE_|EXPECT_|PARTY_PREMIUM$|QUALITY_SCALE$)/.test(key))),
          inputs: { round_index_base: 0, players: everyone(match), teams: match.teams, mm: match.mm, stats: combatStats(match), combatState: match.combatState,
            combat_segments:match.combat_segments,host_migrations:match.host_migrations,
            rounds: match.rounds, kills: match.kills, left: match.left },
          board: scoreboardOf(match), publicMatch: full, rows: settled,
          match_id: id, host, participants: ids, collected_at: now, close_after: now + 5000,
          data_collected: true, full, ratings: {}, history: {}, events: {} };
        const keys = [settlementKey(id)], types = ['string'];
        const add = (key, type) => { keys.push(key); types.push(type); return keys.length; };
        const plan = { id, receipt: canonical, types, ttl: MATCH_TTL_SECONDS, history_ttl: HISTORY_TTL_SECONDS,
          keep: HISTORY_KEEP, writes: [], histories: [], board: [], hashes: [], rank_checks: [],
          result_index: add(resultKey(id), 'string'),
          live: add(liveMatchKey(id), 'string'), live_index: add(liveIndexKey(), 'set'),
          analytics_outbox: add(`${socialPrefix}analytics:outbox`, 'set') };
        plan.authority=add(authorityKey(id),'string');plan.host=host;plan.host_epoch=match.host_epoch||0;
        plan.writes.push({ index: add(matchKey(id), 'string'), value: JSON.stringify(full), ttl: MATCH_TTL_SECONDS });
        if(duel && match.terminal?.reason==='reconnect_timeout' && (!noShowPenaltiesPaused()||match.terminal.recovery===true)){
          const loser=match.terminal.loser,key=penaltyKey(loser),raw=await store(['GET',key],{strict:true});
          const prior=raw?identity.parse(raw):{},count=effectiveCount(prior)+1,seconds=rungSeconds(count);
          // The ordinary loss supplies the RR change; save the escalating queue cooldown
          // in the same transaction so failed/voided results never leave a separate charge.
          const penalty={until:now+seconds*1000,last:now,reason:'reconnect_timeout',count,elo:prior.elo||0};
          canonical.penalties={[loser]:penalty};
          const index=add(key,'string');plan.string_checks=[{index,expected:raw||false}];
          plan.writes.push({index,value:JSON.stringify(penalty)});
        }
        const boardIndex = add(boardKey(), 'zset');
        for (const row of settled) {
          const sid = row.steamId, team = side.get(sid) + 1;
          const clean = ratingLib.normalise(row.after);
          canonical.ratings[sid] = clean;
          // The receipt retains both rows and events; copying every round board
          // into each would exceed storage request limits in a full-size match.
          delete row.event.round_details;
          canonical.events[sid] = { ...row.event, mode:mode.id, terminal:match.terminal||null, data_collected: true, close_allowed: true, close_after: canonical.close_after };
          if(match.recovery||match.terminal?.absence)canonical.events[sid].players=everyone(match).map(p=>({steam_id:p.player_id,
            name:p.persona||p.player_id,team:side.get(p.player_id)+1,left:!(match.players||[]).some(a=>a.player_id===p.player_id)}));
          const h = { ...historyRow(match, record, sid), ...(match.terminal?{outcome:'played',reason:match.terminal.reason,
            recovery_forfeit:(match.terminal.recovery===true||match.terminal.absence===true)&&match.terminal.reason==='reconnect_timeout'}:{}), won: draw ? null : team === winner, draw,
            score: score ? `${score[team]}-${score[team === 1 ? 2 : 1]}` : null, rr_delta: row.rr.delta,
            delta: ratingLib.arrowsFor(row.rr.delta), placement: Boolean(row.rr.placing || row.rr.placed) };
          canonical.history[sid] = h;
          plan.histories.push({ index: add(historyKey(sid), 'list'), value: JSON.stringify(h) });
          if (!draw) {
            const index = add(ratingKey(sid), 'string');
            plan.rank_checks.push({ index, expected: row.before.revision || 0 });
            plan.writes.push({ index, value: JSON.stringify(clean) });
            plan.board.push({ index: boardIndex, member: sid, value: ratingLib.isPlacing(clean) ? false : boardScore(clean) });
          }
        }
        canonical.careers = creditMatch(match, { played: true, dryRun: true, now: match.final_ended_at });
        const rosterIndex = add(rosterKey(), 'hash');
        for (const [sid, rec] of Object.entries(canonical.careers)) {
          const rating = canonical.ratings[sid];
          Object.assign(rec, { rated: rating.matches || 0, wins: rating.wins || 0, losses: rating.losses || 0,
            mmr: Math.round(rating.rating || 0), progress: rating.progress || 0, result_revision: now });
          plan.hashes.push({ index: rosterIndex, field: sid, value: JSON.stringify(rec) });
        }
        const saved = await require('./result-commit.cjs').commit(store, keys, plan);
        await acceptCommitted(match, saved);
        return { ok: true, match_id: id, data_collected: true, close_allowed: true };
      });
      resultSaves.set(id, save);
      try { return await save; } finally { resultSaves.delete(id); }
  }

  async function finishDuelDecision(match) {
    let terminal=match.terminal;
    if((!duel&&!terminal?.recovery&&!terminal?.absence)||!terminal||!['concede','reconnect_timeout'].includes(terminal.reason))return {ok:false,error:'No match decision.'};
    try {
      await persistLive(match);
      if(matches.get(match.id)!==match)return {ok:true,match_id:match.id,data_collected:true,close_allowed:true};
      if(match.void_pending)return finishVoidVote(match);
      terminal=match.terminal;
      const ids=everyone(match).map(p=>p.player_id),assigned=match.assigned_teams;
      if(!ids.includes(terminal.loser)||!assigned?.[1]?.length||!assigned?.[2]?.length||
         (duel&&(ids.length!==2||assigned[1].length!==1||assigned[2].length!==1)))throw Error('Invalid decision roster');
      if(terminal.recovery&&(!match.recovery?.roster?.done||[1,2].filter(side=>
        match.players.some(p=>assigned[side].includes(p.player_id))).join(',')!==String(terminal.winner)))throw Error('Invalid recovery forfeit');
      if(terminal.absence&&[1,2].filter(side=>match.players.some(p=>assigned[side].includes(p.player_id))).join(',')!==String(terminal.winner))throw Error('Invalid absence forfeit');
      const side=new Map([...assigned[1].map(x=>[x,0]),...assigned[2].map(x=>[x,1])]);
      const score=terminal.score,total=score ? score[1]+score[2] : null;
      match.score=score;match.final_ended_at=terminal.at;
      const mergedRows=(match.stats?.players||[]).map(row=>({...row,stats_complete:false}));
      return await commitPlayedResult(match,{ids,side,winner:terminal.winner,draw:false,score,total,
        limit:match.expected_score_limit ?? match.agreedScoreLimit ?? mode.scoreLimit,host:match.host,id:match.id,mergedRows});
    }catch{return {ok:false,unavailable:true,error:'Match decision is being saved. Please retry.'};}
  }
  async function decideDuel(match,loser,reason) {
    if(match.recovery?.phase==='restoring')return {ok:false,error:'Wait for match recovery to finish before conceding.'};
    if(!duel||!match.start_ready_verified||match.state!=='live'||match.void_pending||match.final_snapshot||match.collecting||match.finished)
      return {ok:false,error:'The match cannot be conceded now.'};
    if(match.terminal && (match.terminal.loser!==loser || match.terminal.reason!==reason))return {ok:false,error:'The match is already decided.'};
    if(!match.terminal){
      const team=[1,2].find(n=>match.assigned_teams?.[n]?.includes(loser));
      if(!team)return {ok:false,error:'Invalid duel player.'};
      const score=match.score;
      const limit=match.expected_score_limit ?? match.agreedScoreLimit ?? mode.scoreLimit;
      if(score && ![1,2].every(n=>Number.isSafeInteger(score[n])&&score[n]>=0&&score[n]<limit))return {ok:false,error:'The match is already decided.'};
      match.terminal={reason,loser,winner:team===1?2:1,at:Date.now(),score:score ? {1:score[1],2:score[2]} : null};
      clearTimeout(match.timer);clearTimeout(match.collectTimer);match.timer=null;match.collectTimer=null;
    }
    return finishDuelDecision(match);
  }
  function concedeMatch(account,body){
    const id=String(body?.match_id||'');
    return matchOperation(id,async()=>{
      if(!duel||!identity.validPlayer(account?.player_id))return {ok:false,error:'Not a 1v1 match.'};
      const receipt=await readReceipt(id);
      if(receipt){const full=receipt.publicMatch;identity.freezeMatch(full);
        return identity.gameFor(full,account.player_id)===account.game_steam_id
          ?{ok:true,match_id:id,data_collected:true,close_allowed:true}:{ok:false,error:'Not a match participant.'};}
      const match=matches.get(id),player=account.player_id;
      if(!match||inMatch.get(player)!==id||!identity.validBindings(match)||identity.gameFor(match,player)!==account.game_steam_id)
        return {ok:false,error:'Not a match participant.'};
      try{await refreshAuthority(match);return await decideDuel(match,player,'concede');}
      catch{return {ok:false,unavailable:true,error:'Match decision is being saved. Please retry.'};}
    });
  }

  function finalSnapshot(hostId, fields) {
    return matchOperation(String(fields?.match_id || ''), () => applyFinalSnapshot(hostId, fields));
  }
  async function applyFinalSnapshot(hostId, fields) {
    const host = String(hostId || ''), id = String(fields?.match_id || '');
    const reject = error => ({ ok: false, error, data_collected: false, close_allowed: false });
    if (!identity.validPlayer(host) || !/^[0-9a-f]{16}$/.test(id)) return reject('invalid identity');
    try {
      const receipt = await readReceipt(id);
      if (receipt) {
        if (receipt.host !== host) return reject('not the host');
        await acceptCommitted(matches.get(id), receipt);
        return { ok: true, match_id: id, data_collected: true, close_allowed: true };
      }
      const match = matches.get(id);
      if (!match || match.host !== host || inMatch.get(host) !== id || match.state !== 'live' || !match.start_ready_verified)
        return reject('no confirmed live match');
      if(match.terminal)return finishDuelDecision(match);
      if (!match.combat_end || String(fields.combat_end || '') !== `${match.combat_end.epoch};${match.combat_end.seq}`)
        return reject('combat capture is still draining');
      const assigned = match.assigned_teams;
      const ids = [...(assigned?.[1] || []), ...(assigned?.[2] || [])];
      if(duel&&(ids.length!==2||assigned[1].length!==1||assigned[2].length!==1))return reject('invalid duel assignment');
      if (ids.length < (soloMatch(match)?1:2) || ids.length > 10 || new Set(ids).size !== ids.length) return reject('invalid assignment');
      const side = new Map([...assigned[1].map(x => [x, 0]), ...assigned[2].map(x => [x, 1])]);
      const meta = /^(\d{1,2});(\d{1,2});([01]);(\d{1,2});([01]);(\d{1,2})$/.exec(String(fields.meta || ''));
      if (!meta || meta[3] === meta[5]) return reject('invalid final score');
      const [round, limit, t0, s0, t1, s1] = meta.slice(1).map(Number);
      const score = { [t0 + 1]: s0, [t1 + 1]: s1 };
      const total = s0 + s1;
      if (limit !== match.expected_score_limit || total > match.expected_max_rounds) return reject('wrong game rules');
      const draw = total === match.expected_max_rounds && s0 === s1 && s0 < limit;
      const winner = score[1] === limit && score[2] < limit ? 1 : score[2] === limit && score[1] < limit ? 2 : 0;
      if ((!winner && !draw) || limit < 1 || round < total - 1 || round > total) return reject('not a terminal score');
      if (match.collecting && (match.collecting.winner !== winner || match.collecting.score[1] !== score[1] || match.collecting.score[2] !== score[2]))
        return reject('final score disagrees with decision');
      let text = String(fields.rows || ''); if (text.endsWith(',')) text = text.slice(0, -1);
      const encoded = soloMatch(match)?require('./private-solo.cjs').finalHumans(text.split(','),privateSoloSteam):text.split(',');
      if(!encoded)return reject('invalid private bot roster');
      if (encoded.length < 1 || encoded.length > ids.length) return reject('incomplete final roster');
      const seen = new Set(), finalRows = [];
      const reconnect = require('./reconnect.cjs');
      const current = new Set(match.players.map(p => p.player_id));
      const left = new Set(ids.filter(x => reconnect.confirmedLeft(match,x)));
      const ended=match.final_ended_at||match.collecting?.since||Date.now();
      if(ids.some(x=>current.has(x) && match.reconnect?.[x]?.deadline<=ended))
        return reject('expired reconnect must be adjudicated');
      const absent = new Set(ids.filter(x => {
        const w=match.reconnect?.[x];
        return current.has(x) && w && Number.isSafeInteger(w.since) && w.deadline===w.since+reconnect.GRACE_MS && w.deadline>ended;
      }));
      const unavailable=new Set([...left,...absent]);
      const all=everyone(match).map(p=>p.player_id);
      if (all.length !== ids.length || new Set(all).size !== ids.length || ids.some(x => !all.includes(x)) ||
          ids.some(x => !current.has(x) && !left.has(x)) || [...current].some(x => inMatch.get(x) !== id))
        return reject('assigned roster changed');
      for (const line of encoded) {
        // Every required value must be explicit and bounded. No partial parser success.
        const m = /^(\d{17})\|k=(-?\d{1,5});d=(\d{1,5});sp=(\d{1,5});t=([01]);s=(-?\d{1,5});a=(true|false)$/i.exec(line);
        const player = m && identity.playerFor(match, m[1]);
        if (!m || !side.has(player) || seen.has(player) || Number(m[5]) !== side.get(player)) return reject('invalid final row');
        seen.add(player);
        finalRows.push({ steamId: player, gameSteamId: m[1], kills: Number(m[2]), deaths: Number(m[3]), spawnCount: Number(m[4]),
          teamId: Number(m[5]), teamScore: Number(m[6]), alive: m[7].toLowerCase() === 'true' });
      }
      if(ids.some(x => !seen.has(x) && !left.has(x) && !absent.has(x)))return reject('incomplete final roster');
      finalRows.sort((a, b) => a.steamId.localeCompare(b.steamId));
      const fingerprint = JSON.stringify({ meta: [round, limit, score[1], score[2]], rows: finalRows.filter(row=>!unavailable.has(row.steamId)) });
      if (match.final_snapshot && match.final_snapshot !== fingerprint) return reject('final snapshot changed');
      if (resultSaves.has(id)) return await resultSaves.get(id);
      match.final_snapshot = fingerprint;
      const receivedAt=Date.now(),decidedAt=match.collecting?.since;
      match.final_ended_at ||= Number.isSafeInteger(decidedAt)&&decidedAt>0&&decidedAt<=receivedAt?decidedAt:receivedAt;
      clearTimeout(match.timer); match.timer = null; match.deadline = 0; match.expiry = '';
      for (const sid of ids) frozenCareers.add(sid);
      const priorRows = new Map((match.stats?.players || []).map(row => [row.steamId, row]));
      const freshRows = new Map(finalRows.map(row=>[row.steamId,row]));
      const mergedRows = ids.map(sid => {
        // A departed PlayerState may linger in the final array. Its stale counters cannot
        // overwrite the last totals frozen when the server confirmed the departure.
        if(unavailable.has(sid) || !freshRows.has(sid))return {
          ...(reconnect.confirmedLeft(match,sid)?.last_stats || priorRows.get(sid) || {}),
          steamId:sid,gameSteamId:identity.gameFor(match,sid),teamId:side.get(sid),disconnected:true,stats_complete:false};
        return {...(priorRows.get(sid)||{}),...freshRows.get(sid),stats_complete:true};
      });
      const series = structuredClone(match.stats?.series || {});
      for (const row of finalRows) {
        if(unavailable.has(row.steamId))continue;
        const samples = series[row.steamId] ||= {};
        samples[total - 1] = {...(samples[total - 1] || {}), ...row};
      }
      match.stats = { ...(match.stats || {}), players: mergedRows, series, round_index_base:0, rounds: total, limit };
      match.score = score;
      return await commitPlayedResult(match,{ids,side,winner,draw,score,total,limit,host,id,mergedRows});
    } catch (err) {
      if (String(err.message).includes('result rank conflict')) {
        for (const p of matches.get(id)?.players || []) { ratingLoaded.delete(p.player_id); ratings.delete(p.player_id); }
      }
      console.error('[result] save pending %s: %s', id, err.message);
      // A ban on another worker can atomically supersede this unsaved score.
      if (String(err.message).includes('saved void decision') && matches.has(id)) {
        try { await persistLive(matches.get(id)); } catch { /* next report retries */ }
      }
      return reject('final data is not saved yet');
    }
  }

  /**
   * A match was played to a decision. Settle the ratings, write the result onto the record that
   * was archived at go-live, tell everyone, and let the ten players go.
   *
   * Ordered so that nothing optional can cost anybody their exit: the ratings move and the
   * players are released whatever the history write does.
   */
  function finishMatch(match, winner, score, limit) {
    if (match.start_ready_verified) return null; // Only the explicit full-data receipt completes these matches.
    if (match.finished) return null;
    if (store) return finishDurably(match, winner, score, limit);
    const settled = settleMatch(match, winner) || [];
    return completeMatch(match, winner, score, limit, settled);
  }

  function finishDurably(match, winner, score, limit) {
    if (match.settling) return match.settling;
    match.collecting = match.collecting || { winner, score, limit, since: Date.now(), deadline: Date.now() };
    match.score = score;
    match.score_limit = limit;
    if (match.timer) clearTimeout(match.timer);
    if (match.collectTimer) clearTimeout(match.collectTimer);
    match.timer = match.collectTimer = null;
    // Persist the decided result before committing it. Later flushes skip an
    // active commit, so an older snapshot cannot resurrect a completed match.
    const snapshot = persistLive(match);
    snapshot.catch(() => {}); // observed below after any preceding rank operation
    const ids = everyone(match).map(p => p.player_id);
    match.settling = withRatingLocks(ids, async () => {
      await snapshot;
      if (match.void_pending) return null;
      // A different container may already have durably decided this match.
      ({ winner, score, limit } = match.collecting);
      for (let attempt = 0; attempt < 3; attempt += 1) {
        await loadRatings(ids);
        await Promise.all(ids.map(drainRatingWrites));
        const calculated = settleMatch(match, winner, { calculate: true });
        for (const row of calculated) row.after.revision = (row.before.revision || 0) + 1;
        const receipt = {
          mode:mode.id, analytics_context: analyticsContext(match),
          version: settlementLib.VERSION, matchId: match.id, host:match.host,host_epoch:match.host_epoch||0, at: Date.now(), winner, score, limit,
          inputs: { players: everyone(match), teams: match.teams, mm: match.mm, stats: match.stats, rounds: match.rounds,
                    kills: match.kills, left: match.left },
          rules: Object.fromEntries(Object.entries(process.env).filter(([key]) => /^COMP_(RR_|PERF_|RANK_|RATING_|LEVEL_THRESHOLDS$|ARROW_|PLACEMENT_|REAPER_|W_|WEIGHT_|DECISIVE_|INT_|PRESENCE_|EXPECT_|PARTY_PREMIUM$|QUALITY_SCALE$)/.test(key))),
          board: scoreboardOf(match),
          publicMatch: fullRecord(match, { ended: Date.now(), outcome: 'played', map: match.map, host: match.host }),
          rows: calculated,
        };
        const updates = calculated.map(row => ({ id: row.steamId, expected: row.before.revision || 0,
          json: JSON.stringify(row.after), progress: boardScore(row.after), placing: ratingLib.isPlacing(row.after) }));
        let committed;
        try {
          committed = await settlementLib.commit(store,
            [settlementKey(match.id), boardKey(), liveMatchKey(match.id), liveIndexKey(), ...calculated.map(r => ratingKey(r.steamId)), `${socialPrefix}analytics:outbox`,authorityKey(match.id)],
            receipt, updates);
        } catch (error) {
          if (!error.conflict || attempt === 2) throw error;
          for (const id of ids) { ratingLoaded.delete(id); ratings.delete(id); }
          continue;
        }
        const settled = committed.receipt.rows;
        hydrateSettlement(settled);
        const gap = Math.abs(score[1] - score[2]) / Math.max(score[1], score[2], 1);
        publishSettlement(match, committed.receipt.winner, settled, gap);
        persisted.delete(match.id);
        return completeMatch(match, committed.receipt.winner, committed.receipt.score, committed.receipt.limit, settled);
      }
    }).catch(() => {
      // Keep the match and decided score; the regular tick retries. No changed
      // rank or success event is published until Redis confirms a receipt.
      note(match, 'settlement-pending', {});
      if (match.collecting) match.collecting.deadline = Date.now() + 2000;
      return null;
    }).finally(() => { match.settling = null; });
    return match.settling;
  }

  function hydrateSettlement(rows) {
    for (const row of rows) {
      const current = ratingOf(row.steamId);
      if ((current.revision || 0) < (row.after.revision || 0)) {
        ratings.set(row.steamId, ratingLib.normalise(row.after));
        ratingLoaded.add(row.steamId);
        noteRating(row.steamId, row.after);
      }
    }
  }

  function completeMatch(match, winner, score, limit, settled) {
    match.finished = { at: Date.now(), winner, score, limit };
    if (match.timer) clearTimeout(match.timer);
    match.timer = null;
    if (match.collectTimer) clearTimeout(match.collectTimer);
    match.collectTimer = null;
    match.collecting = null;
    match.state = 'over';

    for (const p of match.players) if (inMatch.get(p.player_id) === match.id) inMatch.delete(p.player_id);
    matches.delete(match.id);
    forgetMatch(match.id);          // and the live copy, so no boot can bring it back
    for (const p of match.players) {
      sendTo(p.player_id, { type: 'match_over', match_id: match.id,
                           won: teamOf(match, p.player_id) === winner, score });
    }
    // The record was written as `played` with null score and null winner the moment the match
    // went live. Now it can say what happened. Best effort, like every other history write.
    patchResult(match, winner, score, settled);
    // And into the directory: the kills, the rounds and the minutes, which nothing else writes
    // down. Not the wins - the rating record owns those and the mirror already carries them.
    creditMatch(match, { played: true });
    broadcast(stats());
    drainQueue();
    return settled;
  }

  /**
   * Fill the result into the record archived at go-live, and into each player's history row.
   *
   * The row is found by scanning the player's list for this match id rather than assuming it is
   * at index 0: it is usually the newest, but a cancelled match they were pulled into while this
   * one ran would sit on top of it. The list is capped at HISTORY_KEEP, so the scan is bounded.
   */
  function patchResult(match, winner, score, settled) {
    try {
      // THE VISIBLE MOVE, NOT THE HIDDEN ONE. This used to be built from `row.delta` - the MMR
      // change - and squashed into arrows, which the history screen printed with "RR" after it.
      // Now `delta` stays the arrows (drawn from RR, like the result event's) for whatever still
      // draws arrows, and `rr_delta` is the number. `placement` marks a match played while
      // placing, whose RR is 0 by design, so a list can say so instead of printing "0 RR".
      const moves = new Map(settled.map((row) => [row.steamId, row.rr || {}]));
      const rrDeltaOf = (steamId) => {
        const rr = moves.get(steamId);
        return rr && Number.isFinite(rr.delta) ? rr.delta : null;
      };
      const moveOf = (steamId) => ({
        delta: ratingLib.arrowsFor(rrDeltaOf(steamId) || 0),
        rr_delta: rrDeltaOf(steamId),
        placement: Boolean((moves.get(steamId) || {}).placing || (moves.get(steamId) || {}).placed),
      });
      const full = archived.get(match.id);
      const board = scoreboardOf(match);
      if (full) {
        full.won_team = winner;
        full.score = { 1: score[1], 2: score[2] };
        // The sweep has been running for the whole match and its last rows arrived during the
        // collection window, so THIS is the moment the board is complete.
        full.scoreboard = board;
        full.round_details = roundDetails(match);
        full.round_index_base = 0;
        full.rounds_played = full.round_details.length || null;
        for (const p of full.players || []) {
          const team = teamOf(match, p.player_id);
          p.won = team === winner;
          Object.assign(p, moveOf(p.player_id));
        }
      }
      const lineOf = (steamId) => {
        const mine = board.find((r) => r.player_id === steamId);
        return mine ? { kills: mine.kills, deaths: mine.deaths, team_kills: mine.team_kills }
                    : { kills: null, deaths: null, team_kills: null };
      };
      for (const [steamId, rows] of history) {
        const row = (rows || []).find((r) => r && r.id === match.id);
        if (!row) continue;
        Object.assign(row, lineOf(steamId));
        row.won = teamOf(match, steamId) === winner;
        row.score = `${score[teamOf(match, steamId)] || 0}-${score[teamOf(match, steamId) === 1 ? 2 : 1] || 0}`;
        Object.assign(row, moveOf(steamId));
      }
      if (!store) return;
      const writes = [];
      if (full) {
        writes.push(store(['SET', matchKey(match.id), JSON.stringify(full),
                           'EX', String(MATCH_TTL_SECONDS)]));
      }
      for (const p of everyone(match)) {
        const steamId = p.player_id;
        const key = historyKey(steamId);
        writes.push(Promise.resolve(store(['LRANGE', key, '0', String(HISTORY_KEEP - 1)]))
          .then((raw) => {
            if (!Array.isArray(raw)) return null;
            for (let i = 0; i < raw.length; i += 1) {
              let row;
              try { row = typeof raw[i] === 'string' ? identity.parse(raw[i]) : raw[i]; } catch { continue; }
              if (!row || row.id !== match.id) continue;
              const mine = teamOf(match, steamId);
              Object.assign(row, lineOf(steamId));
              row.won = mine === winner;
              row.score = `${score[mine] || 0}-${score[mine === 1 ? 2 : 1] || 0}`;
              Object.assign(row, moveOf(steamId));
              return store(['LSET', key, String(i), JSON.stringify(row)]);
            }
            return null;
          }));
      }
      Promise.all(writes).catch(() => { /* the in-memory copy still answers this process */ });
    } catch { /* a result that could not be written down is still a result that was applied */ }
  }

  // ---------------------------------------------------------------- settling a result
  /**
   * A match was WON. Move every rating in it and tell the ten players what it cost them.
   *
   * Called by `gameReportedScore` below, when the gamemode's own scoreboard says a team has
   * reached the score limit.
   *
   * CORRECTION (2026-09-15). This carried a note saying nothing called it and nothing could,
   * "because the gamemode cannot report a scoreboard". That was wrong, and it had been copied
   * into three documents. What `docs/autojoin.md` settled is the OTHER direction: the backend
   * can never send data TO the game, because the response delegate is `OneParam(bool)`.
   * Outbound was always arbitrary - `SendAttributionEvent` has been reaching /api/probe and
   * driving `gameReportedIn` for a day. `ABodycamGameState` exposes `GetTeams()` (an array of
   * `{TeamID, TeamScore, PlayerCount}`) and `GetScoreLimit()`, both BlueprintPure. So the score
   * can be read, and the score can be sent.
   *
   * Settling against `match.mm.ratings` - what each side was worth at FORMATION - rather than
   * against the roster's ratings now, because those have since moved and reconstructing them
   * afterwards is exactly the kind of thing that is quietly wrong for a whole season.
   *
   * `weight` is how much the match is allowed to teach us. A match the matchmaker knowingly
   * formed lopsided - because somebody had waited out their tolerance and any game beat no
   * game - is weaker evidence than a match it was proud of, and moves the ladder less.
   */
  function settleMatch(match, winnerTeam, { calculate = false } = {}) {
    const won = Number(winnerTeam);
    if (!match || (won !== 1 && won !== 2)) return null;
    if (store && !calculate) return finishMatch(match, won, match.score || {}, match.score_limit || DEFAULT_SCORE_LIMIT);
    const mm = match.mm || {};
    const teams = match.teams || (mm.teams || {});
    const before = new Map();
    for (const p of everyone(match)) before.set(p.player_id, ratingOf(p.player_id));
    if(soloMatch(match)) {
      const steamId=match.host, was=before.get(steamId), victory=teamOf(match,steamId)===won;
      const now={...was,matches:(was.matches||0)+1,wins:(was.wins||0)+(victory?1:0),losses:(was.losses||0)+(victory?0:1)};
      const rows=[{steamId,before:was,after:now,delta:0,rr:{delta:0,progress:was.progress||0},won:victory}];
      return calculate?rows:publishSettlement(match,won,rows,0);
    }

    // The two sides as the matchmaker saw them when it made the match. Falling back to the
    // roster's ratings NOW is the reconstruction this function exists to avoid, but it is
    // better than refusing to settle a match that predates `mm`.
    const sideRating = (n) => {
      if (mm.ratings && mm.ratings[n]) return mm.ratings[n];
      const ids = (teams[n] || teams[String(n)] || []);
      return ratingLib.teamAggregate(ids.map((id) => before.get(id) || ratingOf(id)));
    };
    const opponentOf = { 1: sideRating(2), 2: sideRating(1) };

    // THE VALUATION (Sam, 2026-09-15). What each player's match was worth, as a Glicko score
    // and a weight - see valuation.cjs for the whole model. Everything the game reported
    // (`match.stats`) and everything the server knows on its own goes in; with nothing reported
    // it returns the bare outcome, which is exactly what this function used to do by hand.
    const ratingsBefore = {};
    for (const [steamId, record] of before) ratingsBefore[steamId] = record;
    const rows = valuationLib.valuation(match, combatStats(match), ratingsBefore, won);
    const byPlayer = new Map(rows.map((r) => [r.steamId, r]));

    // FACTOR 2, shared by both ladders: how decisive the scoreline was, 0 (coin toss) to 1
    // (whitewash). Computed once here so the hidden and the visible ladder cannot disagree
    // about how emphatic the same match was.
    const score = match.score || {};
    const top = Math.max(Number(score[1]) || 0, Number(score[2]) || 0, 1);
    const roundDiff = Math.min(1, Math.abs((Number(score[1]) || 0) - (Number(score[2]) || 0)) / top);

    const settled = [];
    for (const n of [1, 2]) {
      for (const steamId of (teams[n] || teams[String(n)] || [])) {
        const was = before.get(steamId) || ratingOf(steamId);
        const val = byPlayer.get(steamId)
          || { score: n === won ? 1 : 0, weight: 1, rrWeight: 1, breakdown: { measured: false, excess: 0 } };

        // THE HIDDEN LADDER: Glicko-2, moved by the valuation's score and weight. Riot's rule
        // that a player "can gain MMR on a loss" lives here - a strong loser is handed a score
        // above a heavy underdog's expectation, and Glicko does the rest on its own.
        const aged = ratingLib.ageUncertainty(was, Date.now());
        const now = ratingLib.update(aged, opponentOf[n], val.score, val.weight, sideRating(n));
        progressLib.recordPlacement(was, now, val.breakdown);

        // THE VISIBLE LADDER: RR, chasing the MMR that update() just produced. All four of
        // Riot's factors are inside `award` - outcome, round differential, performance bonus
        // (faded out towards the top) and convergence.
        const rr = progressLib.award(was, now, {
          won: n === won,
          roundDiff,
          excess: (val.breakdown && val.breakdown.excess) || 0,
          // `rrWeight`, NOT `val.weight`. That one is how much the match may teach the hidden
          // rating, and it carries the matchmaker's quality: passing it here is what held Sam's
          // test nights to +8 RR a win. See valuation.cjs.
          weight: Number.isFinite(val.rrWeight) ? val.rrWeight : 1,
          demoteArmed: was.demoteArmed,
        });
        now.progress = rr.progress;
        now.demoteArmed = rr.demoteArmed || false;

        if (!calculate) saveRating(steamId, now);
        settled.push({
          steamId, before: was, after: now,
          delta: now.rating - was.rating,          // MMR movement, hidden
          rr,                                      // RR movement, visible
          valuation: val,
          won: n === won,
        });
      }
    }
    if (calculate) return settled;
    return publishSettlement(match, won, settled, roundDiff);
  }

  function publishSettlement(match, won, settled, roundDiff, dryRun = false) {
    const rounds = roundDetails(match);
    // THE SCOREBOARD RIDES WITH THE RESULT.
    //
    // It is already built at this moment - the collection window held the match open precisely so
    // the last stat sweep would land before settling - and the alternative for the post-match card
    // is a round trip to /api/match/history the instant ten people's matches end.
    // Display-only round summaries accompany it so both views have the same detail;
    // raw combat events are never sent to the client.
    //
    // The SAME array goes to all ten: nothing in it is per-recipient, and `is_me` is the client's
    // own business - it knows its SteamID and the server would otherwise send ten near-identical
    // boards. `persona` is resolved here because the hub has no name for a player it never shared
    // a lobby roster with.
    //
    // Empty when the gamemode reported nothing, which is what the card's honest "no scoreboard"
    // line is for. Never a row of zeroes.
    const board = scoreboardOf(match).map((r) => ({
      player_id: r.player_id,
      persona: personaOf(r.player_id),
      team: r.team,
      reported: r.reported,
      disconnected: r.disconnected,
      stats_complete: r.stats_complete,
      kills: r.kills,
      deaths: r.deaths,
      team_kills: r.team_kills,
      combat: r.combat,
    }));

    // AFTERWARDS is the only time a number goes out (Sam, 2026-09-15). The arrows are drawn from
    // the RR change, not the MMR change: RR is the ladder the player can see, so it is the one the
    // arrows must agree with, or a player gets three green arrows on a match that moved their rank
    // down.
    //
    // AND THE RR ITSELF GOES OUT, as `rr_delta`. The hub has shown RR as a number at every rank
    // since the badge learned to ("Operator II · 45 RR"), but the result only ever carried arrows -
    // and the post-match card printed that 1-3 with "RR" after it, so every match read "+1 RR"
    // whatever the RR had really done (Sam, 2026-09-16: "we are only gaining and losing 1-3 RR").
    // `arrows` stays, for everything that still draws arrows.
    // THE CUT MOVED. Every RR total in this match just changed, and the capstone is 150 seats
    // decided by those totals - so re-read it before the badges go out rather than telling
    // whoever just took a seat about it a minute late. Debounced, and nothing waits on it: the
    // results below use whatever cut has landed, which is at worst the one from a minute ago.
    if (!dryRun) refreshReaperCut();
    for (const row of settled) {
      const rank = progressLib.publicProgress(row.after,
                                              { top: isReaper(row.steamId, row.after.progress) });
      const arrows = ratingLib.arrowsFor(row.rr.delta);
      // THE SAME FACTS TWICE, FLAT AND UNDER `you`. Flat is what the hub in the field reads; `you`
      // is the shape docs/match-result.md hop 2 specifies and the one the result card is written
      // against - `_on_result` reads `you.rank_name`, `you.division`, `you.rr` to move the badge,
      // and without it a player who was promoted by THIS match kept their old rank on screen until
      // the hub was restarted and the `rating` event re-sent it. That is worst at the top of the
      // ladder, where the capstone can be taken and lost without the player playing badly at all.
      const you = {
        ...rank,
        arrows,
        // The RR this match moved: 0 through placements, where RR is frozen. `placed` marks the
        // match that finished them, so a card can say where the player landed rather than
        // "no rank change" on the one match that gave them a rank.
        rr_delta: row.rr.delta,
        placed: Boolean(row.rr.placed),
        // The counted figure's move, and where it was. Null below the counting band, where the
        // hub deliberately shows arrows and never a number (docs/match-result.md, "the one rule
        // in the wire itself") - the presence of `bdr` is what switches between the two.
        bdr_delta: rank.bdr === null ? null : row.rr.delta,
        bdr_before: rank.bdr === null ? null : Math.max(0, rank.bdr - row.rr.delta),
        matches: row.after.matches,
        wins: row.after.wins,
        losses: row.after.losses,
      };
      row.event = {
        type: 'match_result', match_id: match.id, won: row.won,
        delta: arrows,
        rr_delta: row.rr.delta,
        placed: Boolean(row.rr.placed),
        ...rank,
        matches: row.after.matches,
        wins: row.after.wins,
        losses: row.after.losses,
        scoreboard: board,
        round_details: rounds,
        round_index_base: 0,
        rounds_played: rounds.length,
        you,
        map: match.map, score: match.score, winner_team: won,
      };
      if (!dryRun) sendTo(row.steamId, row.event);
    }
    if (!dryRun) match.settled = { at: Date.now(), winner: won, roundDiff };
    return settled;
  }

  // ---------------------------------------------------------------- parties
  //
  // `personaOf` / `avatarOf` USED TO BE REDECLARED HERE, and that is the whole of the bug Sam
  // reported. Two `function` declarations of one name in one scope is legal JavaScript: the second
  // silently replaces the first for EVERY caller in the closure, including the ones written
  // against the first. So the weaker pair - live clients only, nothing else - is what the
  // leaderboard and the friends list had been calling since 6e324e8, and everybody offline came
  // out as a 17-digit number. Never a regression, never an error, and invisible to any test that
  // only ever looked at connected players.
  //
  // The party payload wants exactly what the pair above already returns, so there is nothing to
  // replace it with: one definition, near the top, and this note so nobody adds a third.

  function partyCode() {
    const bytes = crypto.randomBytes(6);
    let out = '';
    for (let i = 0; i < 6; i += 1) out += PARTY_CODE_ALPHABET[bytes[i] % PARTY_CODE_ALPHABET.length];
    return `${out.slice(0, 4)}-${out.slice(4)}`;
  }

  /** A code that is not already in use. */
  function freshPartyCode() {
    let code = partyCode();
    while (parties.has(code)) code = partyCode();
    return code;
  }

  /** Accept what a person actually types - spaces, dashes, lower case - the way the hub's
   *  normalise_party_code does, so the two agree on what a valid code is. '' means malformed. */
  function normaliseCode(text) {
    const cleaned = String(text || '').toUpperCase().split('')
      .filter((c) => PARTY_CODE_ALPHABET.includes(c)).join('');
    if (cleaned.length !== 6) return '';
    return `${cleaned.slice(0, 4)}-${cleaned.slice(4)}`;
  }

  /**
   * One party as its members should see it.
   *
   * `level` is REAL now - the rank service exists, so the note that used to stand here ("the
   * server has no rank service... it must never invent a number") is settled by having a
   * number that is not invented. It stays null for a player still in placements, which is the
   * same null the hub already renders as an unknown rank, so nothing on the client changes.
   *
   * `ping` is still an honest null: there is no in-game ping to report.
   */
  function partyPayload(code) {
    const party = parties.get(code);
    if (!party) return { type: 'party_update', code: null };
    return {
      type: 'party_update', code, leader_id: party.leaderId,
      members: party.members.map((id) => ({
        player_id: id, persona: personaOf(id), avatar: avatarOf(id),
        level: ratingLib.levelOf(ratingOf(id)),
        ping: null,
      })),
    };
  }

  function broadcastParty(code) {
    const party = parties.get(code);
    if (!party) return;
    const payload = partyPayload(code);
    for (const id of party.members) sendTo(id, payload);
  }

  /**
   * Take a player out of whatever party they are in. If they were the leader, the party passes
   * to members[0]; if nobody is left, the party is dissolved. The survivors are told; the caller
   * tells the leaver separately ({code:null}, "you are solo now"). Returns the affected code, or
   * null if they were in no party.
   */
  function cancelQueuedFor(ids) {
    const affected = new Set(ids);
    for (const id of ids) {
      const unit = queueOf.get(id);
      if (unit) for (const member of unit.members) affected.add(member);
    }
    for (const id of affected) queueIntents.delete(id);
    for (const id of affected) {
      const unit = removeFromQueue(id);
      if (unit) for (const member of unit.members) sendTo(member, {type:'unqueued', reason:'party_changed'});
    }
    broadcast(stats());
  }

  function leaveParty(steamId) {
    const code = partyOf.get(steamId);
    if (!code) return null;
    const party = parties.get(code);
    cancelQueuedFor(party ? party.members : [steamId]);
    partyOf.delete(steamId);
    if (!party) return null;
    party.members = party.members.filter((id) => id !== steamId);
    if (!party.members.length) {
      parties.delete(code);
      return code;
    }
    if (party.leaderId === steamId) party.leaderId = party.members[0];
    broadcastParty(code);
    return code;
  }

  // A deliberate party action (create/join/leave) proves the caller is present, so any grace timer
  // armed by their last stream closing is stale and must be cleared. Crucially, this is keyed by the
  // CALLER's steam id, not by the party it was armed for: without this, a member whose stream dropped
  // in P1 and who then POSTs join {P2} in the reconnect gap would have the still-pending P1 timer
  // fire and wrongly evict them from P2 (the timer re-reads partyOf, which now points at P2).
  // NOT done in handlePartyRefresh: a leader re-keying for a still-absent member must leave that
  // member's grace timer running so the window can still remove them.
  function clearOwnGrace(steamId) {
    const g = partyGrace.get(steamId);
    if (g) { clearTimeout(g); partyGrace.delete(steamId); }
  }

  function handlePartyCreate(res, account) {
    clearOwnGrace(account.player_id);
    const existing = partyOf.get(account.player_id);
    if (existing && parties.has(existing)) {
      // Already in a party: hand it back idempotently rather than minting a second code and
      // orphaning the first. broadcastParty is not needed - nothing changed.
      return sendJson(res, 200, { ok: true, code: existing });
    }
    if (inMatch.has(account.player_id)) return sendJson(res,409,{ok:false,error:'Finish your match before changing parties.'});
    if(competitionGuard?.canChangeParty?.([account.player_id])===false)
      return sendJson(res,409,{ok:false,error:'Leave the 1v1 queue or finish your match before joining a party.'});
    cancelQueuedFor([account.player_id]);
    const code = freshPartyCode();
    parties.set(code, { code, leaderId: account.player_id, members: [account.player_id], created: Date.now() });
    partyOf.set(account.player_id, code);
    broadcastParty(code);
    return sendJson(res, 200, { ok: true, code });
  }

  /**
   * Put `steamId` into the party `rawCode` names. Returns { status, body } rather than writing a
   * response, because there are two ways in now: typing a code, and accepting an invite. Both have
   * to make the same checks in the same order, and a second copy of them is how one of the two
   * ends up letting an eleventh player into a party of ten.
   */
  function joinPartyByCode(steamId, rawCode) {
    const code = normaliseCode(rawCode);
    // A malformed code and an unknown code are the same 404 to the caller: there is no party
    // there either way, and the hub maps 404 -> "No party with that code."
    if (!code) return { status: 404, body: { ok: false, error: 'No party with that code.' } };
    const party = parties.get(code);
    if (!party) return { status: 404, body: { ok: false, error: 'No party with that code.' } };
    if (party.members.includes(steamId)) {
      return { status: 200, body: { ok: true, code } };      // already in it: idempotent
    }
    if (party.members.length >= MAX_PARTY) {
      return { status: 409, body: { ok: false, error: 'That party is full.' } };
    }
    if ([steamId,...party.members].some(id => inMatch.has(id))) {
      return {status:409,body:{ok:false,error:'Finish your match before changing parties.'}};
    }
    if(competitionGuard?.canChangeParty?.([steamId,...party.members])===false)
      return {status:409,body:{ok:false,error:'Leave the 1v1 queue or finish your match before joining a party.'}};
    cancelQueuedFor([steamId,...party.members]);
    // A join is also a switch: leave whatever party you were in first, so partyOf stays 1:1.
    const prev = partyOf.get(steamId);
    if (prev && prev !== code) leaveParty(steamId);
    party.members.push(steamId);
    partyOf.set(steamId, code);
    broadcastParty(code);
    return { status: 200, body: { ok: true, code } };
  }

  async function handlePartyJoin(req, res, account) {
    clearOwnGrace(account.player_id);
    let body = {};
    try {
      const raw = await readBody(req, 1024);
      if (raw && raw.length) body = identity.parse(raw.toString('utf8'));
    } catch { body = {}; }
    const joined = joinPartyByCode(account.player_id, body && body.code);
    return sendJson(res, joined.status, joined.body);
  }

  // ---------------------------------------------------------------- party invites
  //
  // Sam, 2026-09-15: "have an invite friends button that appears once a party is created and a
  // user can send an invite to a friend ... an invite inbox system where a user receives a game
  // invite where they can decline or accept."
  //
  // An invite is a POINTER, not a promise: it records which party code was offered and by whom,
  // and every check that matters (does the party still exist, is it full, are you already in it)
  // is made again when it is ACCEPTED. That is the only ordering that survives the two things
  // that always happen - the party filling up while the invite sits unread, and the inviter
  // leaving the party they invited you to.

  function invitesFor(steamId) {
    let box = partyInvites.get(steamId);
    if (!box) { box = new Map(); partyInvites.set(steamId, box); }
    return box;
  }

  /** Forget one invite. Returns true if there was one. `notify` pushes the inbox to the invitee. */
  function dropInvite(to, from, notify) {
    const box = partyInvites.get(to);
    const invite = box && box.get(from);
    if (!invite) return false;
    if (invite.timer) clearTimeout(invite.timer);
    box.delete(from);
    if (!box.size) partyInvites.delete(to);
    if (notify) sendTo(to, invitePayload(to));
    return true;
  }

  /**
   * The invitee's inbox as they should see it. Expired rows are dropped on the way out rather
   * than trusted to their timer: a timer that did not fire (a process that was busy, a clock the
   * runtime slept through) must not be able to show a seat that is no longer being offered.
   */
  function invitePayload(steamId) {
    const now = Date.now();
    const box = partyInvites.get(steamId);
    const invites = [];
    if (box) {
      for (const [from, invite] of [...box.entries()]) {
        const party = parties.get(invite.code);
        if (invite.expires <= now || !party) { dropInvite(steamId, from, false); continue; }
        invites.push({
          from: { player_id: from, persona: personaOf(from) },
          code: invite.code,
          size: party.members.length,
          max: MAX_PARTY,
          expires_in: Math.max(0, Math.round((invite.expires - now) / 1000)),
        });
      }
    }
    invites.sort((a, b) => b.expires_in - a.expires_in);
    return { type: 'party_invites', invites };
  }

  /**
   * Invite a friend into the party I am in.
   *
   * FRIENDS ONLY, and only somebody with a live stream. An invite is a thing a stranger could
   * otherwise put on your screen from anywhere, and the whole point of the friends list is that
   * it is the list of people allowed to do that. Any MEMBER may invite, not just the leader: the
   * invite only offers a seat, and the person who has to live with the answer is the one holding
   * the invite, who can decline it.
   */
  async function inviteToParty(meId, body) {
    const me = String(meId || '');
    const target = String((body && body.target) || '');
    if (!identity.validPlayer(me) || !identity.validPlayer(target)) return { ok: false, error: 'not a steamid64' };
    if (target === me) return { ok: false, error: 'That is you.' };

    const code = partyOf.get(me) || '';
    const party = code ? parties.get(code) : null;
    if (!party) return { ok: false, error: 'You are not in a party.' };
    if (party.members.includes(target)) return { ok: true, already: 'member', code };
    if (party.members.length >= MAX_PARTY) return { ok: false, error: 'Your party is full.' };

    await loadFriends(me);
    if (!setOf(friends, me).has(target)) return { ok: false, error: 'They are not on your friends list.' };
    // Presence is not politeness here: an invite is delivered over the target's OWN stream and
    // nothing stores it, so one sent to somebody with the hub closed would simply evaporate.
    if (!bySteam.has(target)) return { ok: false, error: 'They are not online.' };

    const box = invitesFor(target);
    const resend = box.has(me);                      // re-inviting just restarts the clock
    if (!resend && box.size >= MAX_PARTY_INVITES) {
      return { ok: false, error: 'They have too many invites waiting.' };
    }
    if (resend) dropInvite(target, me, false);
    const expires = Date.now() + PARTY_INVITE_SECONDS * 1000;
    const timer = setTimeout(() => { dropInvite(target, me, true); }, PARTY_INVITE_SECONDS * 1000);
    if (timer.unref) timer.unref();                  // never hold the process open for an invite
    box.set(me, { code, at: Date.now(), expires, timer });

    sendTo(target, invitePayload(target));
    // A cue as well as the list: the inbox is on the Competitive screen and the invitee may be
    // looking at any of them, so this is what a toast is drawn from.
    sendTo(target, { type: 'party_invite', from: { player_id: me, persona: personaOf(me) }, code });
    return { ok: true, sent: true, expires_in: PARTY_INVITE_SECONDS };
  }

  /** Take the seat. Every check is made again HERE - see the note above. */
  function acceptPartyInvite(meId, body) {
    const me = String(meId || '');
    const from = String((body && body.from) || '');
    if (!identity.validPlayer(me) || !identity.validPlayer(from)) return { ok: false, error: 'not a steamid64' };
    const box = partyInvites.get(me);
    const invite = box && box.get(from);
    if (!invite) return { ok: false, error: 'That invite has expired.' };
    if (invite.expires <= Date.now() || !parties.get(invite.code)) {
      dropInvite(me, from, true);
      return { ok: false, error: 'That party is gone.' };
    }
    clearOwnGrace(me);
    const joined = joinPartyByCode(me, invite.code);
    if (!joined.body.ok) {
      // Full, or the party vanished between the two checks. Keep the invite: the party may empty
      // out again inside the window, and deleting it here would make a full party look expired.
      return { ok: false, error: joined.body.error || 'Could not join that party.' };
    }
    // Everything pointing at the party I am now in is spent, whoever sent it.
    for (const [other, inv] of [...(partyInvites.get(me) || new Map()).entries()]) {
      if (inv.code === invite.code) dropInvite(me, other, false);
    }
    sendTo(me, invitePayload(me));
    return { ok: true, joined: true, code: invite.code };
  }

  /**
   * No thanks. The inviter is NOT told, the same rule a declined friend request follows: telling
   * them turns a quiet no into a notification whose only use is asking again. They can see the
   * party roster, which is where an accepted invite shows up.
   */
  function declinePartyInvite(meId, body) {
    const me = String(meId || '');
    const from = String((body && body.from) || '');
    if (!identity.validPlayer(me) || !identity.validPlayer(from)) return { ok: false, error: 'not a steamid64' };
    const had = dropInvite(me, from, true);
    return { ok: true, declined: had };
  }

  function handlePartyLeave(res, account) {
    if (inMatch.has(account.player_id)) return sendJson(res,409,{ok:false,error:'Finish your match before changing parties.'});
    clearOwnGrace(account.player_id);
    const was = Boolean(partyOf.get(account.player_id));
    leaveParty(account.player_id);
    sendTo(account.player_id, { type: 'party_update', code: null });    // you are solo now
    return sendJson(res, 200, { ok: true, was_in_party: was });
  }

  function handlePartyRefresh(res, account) {
    const code = partyOf.get(account.player_id);
    const party = code && parties.get(code);
    if (!party) return sendJson(res, 404, { ok: false, error: 'You are not in a party.' });
    if (party.leaderId !== account.player_id) {
      return sendJson(res, 403, { ok: false, error: 'Only the leader can do that.' });
    }
    if (party.members.some(id=>inMatch.has(id))) return sendJson(res,409,{ok:false,error:'Finish your match before changing parties.'});
    cancelQueuedFor(party.members);
    const next = freshPartyCode();
    // Re-key in ONE synchronous block, no await between the parties and partyOf updates: the old
    // code is gone from `parties` and every member's partyOf points at the new code before any
    // other action can run, so a stale join gets the same 404 and no partyOf entry is orphaned
    // (Risks: refresh-code re-keying race).
    parties.delete(code);
    party.code = next;
    parties.set(next, party);
    for (const id of party.members) partyOf.set(id, next);
    broadcastParty(next);
    return sendJson(res, 200, { ok: true, code: next });
  }

  // ---------------------------------------------------------------- routes
  async function handleStream(req, res, account) {
    const clientId = crypto.randomBytes(8).toString('hex');
    // This response is held open for the whole session; no inactivity timeout may close it.
    try { res.setTimeout(0); req.socket.setTimeout(0); } catch { /* not all sockets expose it */ }
    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',        // tell any buffering proxy to let events through
    });
    res.write('retry: 3000\n\n');

    // THE ONE PLACE A NAME ENTERS THIS PROCESS, so it is the one place that has to remember it.
    // `account` is whoami's answer, which is auth's day-cached Steam profile - so every hub that
    // opens its stream refreshes its owner's name for everybody else's leaderboard and friends
    // list, whether or not that player ever queues.
    rememberProfile(account.player_id, account.persona, account.avatar);
    // ...and into the admin directory, which is what makes "every player that has ever logged
    // on" a row rather than only every player who has ever played.
    noteSeen(account.player_id, account.persona, account);
    clients.set(clientId, { res, steamId: account.player_id, gameSteamId: account.game_steam_id, token: bearer(req),
                            accountId:account.account_id, authMethod:account.auth_method, sourceSteamId:account.steam_id, persona: account.persona || '',
                            // The Steam avatar URL, alongside the persona and for the same reason:
                            // it is the only place the rest of the process can learn what someone
                            // looks like. `whoami` already fetched both from the Steam Web API.
                            avatar: account.avatar || '', since: Date.now() });
    if (!bySteam.has(account.player_id)) bySteam.set(account.player_id, new Set());
    bySteam.get(account.player_id).add(clientId);
    // They are back inside the grace window, so the pending "remove from party" never fires: a
    // brief drop is a no-op, and the party replay below hands them the roster again.
    if (partyGrace.has(account.player_id)) {
      clearTimeout(partyGrace.get(account.player_id));
      partyGrace.delete(account.player_id);
    }

    send(clientId, { type: 'hello', player_id: account.player_id, game_steam_id: account.game_steam_id, persona: account.persona || '',
                     match_size: MATCH_SIZE, accept_seconds: ACCEPT_SECONDS,
                     connect_seconds: CONNECT_SECONDS, lobby_seconds: LOBBY_SECONDS,
                     ban_seconds: BAN_SECONDS,
                     pick_seconds: PICK_SECONDS, flip_seconds: FLIP_SECONDS,
                     // The rank ladder, so the hub can EXPLAIN ranked instead of describing a
                     // guess. Every band is an env dial; a client shipping its own copy would go
                     // quietly wrong the day one is turned. Sent with hello because it is static
                     // config, like the other windows above it.
                     //
                     // TWO HALVES, FROM THE TWO MODULES THAT OWN THEM. `ranks` is the VISIBLE
                     // ladder a player climbs (progress.cjs) and is what the hub draws badges and
                     // rank names from; everything else describes the hidden banding underneath.
                     // rating.cjs used to ship both, which is how they came to disagree.
                     ladder: { ...ratingLib.ladder(), ranks: progressLib.ranks() },
                     // THE PENALTY RULES, for exactly the same reason the ladder is here: every
                     // number below is an env dial, so a hub that shipped its own copy would go
                     // on explaining the old ban ladder the day one of them is turned, and
                     // nothing would fail loudly enough to notice. The Penalties screen draws
                     // itself from this and draws nothing at all when it is absent.
                     penalties: {
                       paused: queuePenaltiesPaused(),
                       no_show_paused: noShowPenaltiesPaused(),
                       // The queue-time ladder, rung by rung, in seconds - not the multipliers,
                       // because a client that had to multiply would need the base as well and
                       // could get the arithmetic wrong on its own.
                       rungs: NO_SHOW_LADDER.map((m) => NO_SHOW_BAN_SECONDS * m),
                       // How long a clean run has to be for one rung to come back off.
                       decay_seconds: NO_SHOW_DECAY_SECONDS,
                       // What an offence costs on the VISIBLE ladder. RR since 2026-09-16.
                       rr: NO_SHOW_RR,
                       // The two windows a player can miss, so the screen can say how long they
                       // actually had rather than "a while".
                       accept_seconds: ACCEPT_SECONDS,
                       connect_seconds: CONNECT_SECONDS,
                       // Team killing is on the SAME ladder (reportTeamKill -> applyNoShow), and
                       // `enforced` is COMP_TK_ENFORCE. A screen that promised a ban the server
                       // is not handing out would be the one lie this panel cannot afford.
                       team_kill: { enforced: tkEnforcing(), limit: TK_MATCH_LIMIT,
                                    early_seconds: TK_EARLY_SECONDS },
                     } });
    send(clientId, stats());
    if (competitionGuard) send(clientId,{type:'ranked_ban',ban:banOf(account.player_id)||banOf(account.game_steam_id)||null});
    if (isQueued(account.player_id)) {
      send(clientId, { type: 'queued', position: queuePosition(account.player_id),
                       size: queuedPlayers() });
    }
    // Their rank, as soon as the stream is up - which is also what warms `ratings` for the
    // matchmaker, so the first thing it reads when they queue is not a default. Fire and
    // forget: a rank that could not be read must not hold up a match replay below it.
    //
    // THE VISIBLE RANK, from the same call `match_result` uses. This used to send
    // `ratingLib.publicRating`, which named the rank the player's matchmaking rating deserved - so the
    // hub showed one rank when the stream came up and a different one the moment a match settled,
    // and the badge appeared to move for reasons that had nothing to do with that match.
    //
    // The event is still called `rating` on the wire. It is the installed hub's handler name and
    // renaming it server-side would leave every copy in the field without a rank until it updated.
    loadRating(account.player_id).then((record) => {
      // The capstone is a seat on the leaderboard rather than a band of the ladder, so the badge
      // needs the cut as well as the player's RR. Refreshed here for the same reason the rank is
      // sent here: this is the one place every signed-in hub passes through.
      refreshReaperCut();
      send(clientId, { type: 'rating',
                       ...progressLib.publicProgress(record,
                                                     { top: isReaper(account.player_id,
                                                                     record.progress) }),
                       matches: record.matches, wins: record.wins, losses: record.losses });
    }).catch(() => { /* an unreadable rank is not a reason to lose the stream */ });
    // Reconnecting into a match that is already counting down: hand back the clock rather
    // than leaving the hub to guess how much of the window is left. A transient stream drop
    // during the accept window used to abandon the accept UI and get the player blamed for
    // declining (bug 3), so `found` and `ready` are replayed here too, not only `connecting`.
    //
    // EVERY replay carries `resumed: true`. The hub needs it: a fresh match_ready means "start
    // the lobby", while a resumed one means "a lobby you are not part of is already running
    // elsewhere" - re-running it locally would show this player a coin flip and a veto that
    // nobody else is having. `live` is replayed for the same reason the others are: the hub
    // may have been closed, updated or offline, and the match is still running without it.
    const rejoin = matches.get(inMatch.get(account.player_id));
    if (rejoin && (rejoin.state === 'found' || rejoin.state === 'ready')) {
      const acceptedIds = rejoin.players.filter((p) => p.accepted).map((p) => p.player_id);
      // accepted_ids lets the reconnecting client restore ITS OWN accepted state from server
      // truth rather than resetting it; accept_seconds is what is really left on the clock.
      send(clientId, {
        type: 'match_found', match_id: rejoin.id, resumed: true,
        accept_seconds: Math.max(0, Math.round((rejoin.deadline - Date.now()) / 1000)),
        players: rejoin.players.map((p) => ({ player_id: p.player_id, game_steam_id: identity.gameFor(rejoin, p.player_id), persona: p.persona, avatar: p.avatar || '' })),
        accepted: acceptedIds.length,
        total: rejoin.players.length,
        accepted_ids: acceptedIds,
      });
      if (rejoin.state === 'ready') {
        send(clientId, {
          type: 'match_ready', match_id: rejoin.id, resumed: true,
          lobby_seconds: Math.max(0, Math.round((rejoin.deadline - Date.now()) / 1000)),
          players: rejoin.players.map((q) => ({ player_id: q.player_id, game_steam_id: identity.gameFor(rejoin, q.player_id), persona: q.persona })),
          // The lobby lives here now, so a reconnecting client drops back into the real stage -
          // the toss, the choice, whatever has been banned - instead of waiting blind.
          ...(rejoin.lobby ? lobbyPayload(rejoin) : {}),
        });
      }
    } else if (rejoin && rejoin.state === 'connecting') {
      send(clientId, { ...connectPayload(rejoin, account.player_id), resumed: true });
    } else if (rejoin && rejoin.state === 'live') {
      send(clientId, { ...livePayload(rejoin, account.player_id), resumed: true });
    }
    // Survives a brief reconnect: a member whose stream dropped and came back inside the grace
    // is still in the party (see drop()), so hand them the current roster. This is the whole
    // "party survives a reconnect" contract on the server side.
    if (partyOf.has(account.player_id)) send(clientId, partyPayload(partyOf.get(account.player_id)));
    // ...and the invite inbox, for the same reason: it lives only in this process's memory and on
    // the invitee's screen, so a hub that has just (re)connected has to be handed it or an invite
    // sent while it was away is invisible until somebody sends another one.
    if (partyInvites.has(account.player_id)) send(clientId, invitePayload(account.player_id));
    // Whether or not a ban is running, a player with a record is told what the NEXT no-show
    // would cost. The connect screen warns with that number, so a repeat offender is not
    // promised five minutes and then handed thirty.
    loadPenalty(account.player_id).then(() => {
      const record = penalties.get(account.player_id);
      const live = livePenalty(account.player_id);
      if (!record && !live) return;
      send(clientId, {
        type: 'penalty',
        until: live ? live.until : 0,
        reason: (live && live.reason) || (record && record.reason) || '',
        seconds: live ? Math.max(0, Math.round((live.until - Date.now()) / 1000)) : 0,
        count: effectiveCount(record),
        next_seconds: nextNoShowSeconds(account.player_id),
      });
    }).catch(() => { /* a store hiccup must not break the stream */ });
    broadcast(stats());

    let checking = false;
    const beat = setInterval(async () => {
      const client = clients.get(clientId);
      if (!client) return clearInterval(beat);
      if (checking) return;
      checking = true;
      try {
        const current = await whoami(client.token);
        if (!current || identity.playerOf(current) !== client.steamId || identity.gameOf(current) !== client.gameSteamId) {
          client.res.end(); clearInterval(beat); drop(clientId); return;
        }
      } catch { client.res.end(); clearInterval(beat); drop(clientId); return; }
      finally { checking = false; }
      send(clientId,stats());
      try { client.res.write(': ping\n\n'); } catch { clearInterval(beat); drop(clientId); }
    }, HEARTBEAT_MS);

    req.on('close', () => { clearInterval(beat); drop(clientId); });
    req.on('error', () => { clearInterval(beat); drop(clientId); });
  }

  /**
   * Start searching. THE PARTY IS WHAT QUEUES (C10/C12), not the player who pressed the button.
   *
   * Only the leader may start it - the hub has always gated this client-side (competitive.py
   * `find_match`), and now the server means it too, so a second member hammering the endpoint
   * cannot put the same party in the queue twice. Everything is checked for EVERY member before
   * anybody is enqueued: a party that queues with one member banned or already in a match would
   * form a ten-player match that cannot be accepted, and burn somebody else's accept window
   * finding that out.
   */
  async function handleQueueJoin(res, account, callerToken) {
    const intent = {};
    queueIntents.set(account.player_id,intent);
    try {
    if(duel&&(partyOf.has(account.player_id)||competitionGuard?.inParty?.(account.player_id)))
      return sendJson(res,409,{ok:false,solo_only:true,error:'Leave your party before entering the 1v1 queue.'});
    if (inMatch.has(account.player_id)) {
      return sendJson(res, 409, { ok: false, error: 'You are already in a match.' });
    }
    const code = partyOf.get(account.player_id) || '';
    const party = code ? parties.get(code) : null;
    if (party && party.leaderId !== account.player_id) {
      return sendJson(res, 403, { ok: false, error: 'Only the party leader starts the search.',
                                  not_leader: true });
    }
    const members = party ? party.members.slice() : [account.player_id];
    if (bindingConflict(members)) return sendJson(res,409,{ok:false,error:'Each player must use a different verified game account.'});

    const busy = members.filter((id) => inMatch.has(id));
    if (busy.length) {
      return sendJson(res, 409, { ok: false, error: 'Somebody in your party is already in a match.',
                                  who: busy });
    }
    // The CALLER is obviously present - they are making this request - so only the others are
    // checked for a stream. A member who has closed the hub cannot press Accept, and a party
    // searching with a hole in it is searching for a match it will cancel twenty seconds later.
    const away = members.filter((id) => id !== account.player_id && !bySteam.has(id));
    if (away.length) {
      return sendJson(res, 409, { ok: false, error: 'Somebody in your party is not connected.',
                                  who: away });
    }

    // EVERY MEMBER IS ON THE CURRENT BUILD, or nobody queues. Checked before the bans because
    // it costs nothing (it is a read of what they already told us) and because it is the one
    // refusal here the player can act on immediately.
    //
    // 426 Upgrade Required, and deliberately NOT one of the hub's RETRY_STATUSES: retrying an
    // out-of-date build cannot make it current, and three silent retries would only make the
    // refusal take six seconds to appear.
    const stale = members.map((id) => [id, versionProblem(id)]).filter(([, problem]) => problem);
    if (stale.length) {
      const mine = stale.find(([id]) => id === account.player_id);
      const [who, problem] = mine || stale[0];
      return sendJson(res, 426, {
        ok: false,
        error: mine ? 'Update before you queue.'
                    : 'Somebody in your party is not on the current version.',
        outdated: true,
        ...problem,
        ...(mine ? {} : { who }),
      });
    }

    // Every member's ban, not just the leader's. The caller's own ban keeps exactly the shape
    // the hub already reads (competitive.py `_join_result`); somebody else's says who.
    let bans;
    try { bans = await Promise.all(members.map(async (id) => [id, await loadPenalty(id)])); }
    catch { return sendJson(res, 503, { ok: false, error: 'Could not verify queue eligibility. Please try again.' }); }
    const blocked = bans.filter(([, p]) => Boolean(p));
    if (blocked.length) {
      const mine = blocked.find(([id]) => id === account.player_id);
      const [who, penalty] = mine || blocked[0];
      const seconds = Math.max(1, Math.round((penalty.until - Date.now()) / 1000));
      return sendJson(res, 403, {
        ok: false,
        error: mine ? 'You are banned from the queue.' : 'Somebody in your party is banned from the queue.',
        banned: true, reason: penalty.reason, seconds, until: penalty.until,
        count: penalty.count || 0,
        next_seconds: nextNoShowSeconds(who),
        ...(mine ? {} : { who }),
      });
    }

    // Ratings before the queue, never after: the matchmaker sorts on them the instant the unit
    // lands, and a default 1500/RD350 standing in for a player we have actually measured would
    // put them in the wrong match.
    try { if(duel)await Promise.all(members.map(async id=>recentDuels.set(id,await readHistory(id)))); await loadRatings(members); await Promise.all(members.map(drainRatingWrites)); await Promise.all(members.map(loadBan)); }
    catch {
      return sendJson(res, 503, { ok: false, error: 'Could not load your rank. Please try again.' });
    }
    // An open stream is presence, not proof that its credential is still valid.
    // Recheck every participant after storage reads, then do all state checks below
    // without another await before enqueueing the party.
    try {
      const active = await Promise.all(members.map(async id => {
        if (id === account.player_id) {
          const fresh = await whoami(callerToken);
          return fresh && identity.playerOf(fresh) === id && identity.gameOf(fresh) === gameOfPlayer(id);
        }
        for (const clientId of [...(bySteam.get(id) || [])]) {
          const client = clients.get(clientId);
          const fresh = client?.token && await whoami(client.token);
          if (fresh && identity.playerOf(fresh) === id && identity.gameOf(fresh) === gameOfPlayer(id)) return true;
          if (client) { try {client.res.end();} catch {} drop(clientId); }
        }
        return false;
      }));
      if (active.some(ok=>!ok)) return sendJson(res,409,{ok:false,error:'Somebody in your party needs to sign in again.'});
    } catch { return sendJson(res,503,{ok:false,error:'Could not verify your party. Please try again.'}); }
    // Every await above admits party, cancellation, moderation and match events.
    // Revalidate synchronously before mutating the queue.
    const currentCode = partyOf.get(account.player_id) || '';
    const currentParty = currentCode ? parties.get(currentCode) : null;
    const currentMembers = currentParty ? currentParty.members : [account.player_id];
    if (bindingConflict(members)) return sendJson(res,409,{ok:false,error:'A game account is already playing or searching.'});
    if (queueIntents.get(account.player_id) !== intent || currentCode !== code
        || (currentParty && currentParty.leaderId !== account.player_id)
        || currentMembers.length !== members.length || members.some(id=>!currentMembers.includes(id))
        || members.some(id=>inMatch.has(id) || (id !== account.player_id && !bySteam.has(id)))) {
      return sendJson(res,409,{ok:false,error:'Your party or queue state changed. Please try again.'});
    }
    if (members.some(id=>versionProblem(id) || livePenalty(id) || banOf(id))) {
      return sendJson(res,409,{ok:false,error:'Queue eligibility changed. Please try again.'});
    }
    // A stream's initial read may have failed while storage was unavailable. Refresh its
    // display after recovery instead of leaving it apparently unranked until match end.
    for (const id of members) pushRating(id, ratingOf(id));

    if (networkLib.enforced()) {
      const profiles = members.map(id => networkRegistry.player(id));
      const missing = members.filter((id,i) => !networkLib.ready(profiles[i],Date.now()));
      if (missing.length) return sendJson(res,409,{ok:false,network_unready:true,who:missing,
        error:'Waiting for fresh relay measurements. Check your region in Settings.'});
      if (!networkLib.compatible(profiles)) return sendJson(res,409,{ok:false,network_unready:true,
        error:'Every member of a mixed-region party must allow cross-region matchmaking in Settings.'});
    }

    if(duel&&(partyOf.has(account.player_id)||competitionGuard?.inParty?.(account.player_id)))
      return sendJson(res,409,{ok:false,solo_only:true,error:'Leave your party before entering the 1v1 queue.'});
    const queuedUnit = queueOf.get(account.player_id);
    if (queuedUnit && (queuedUnit.code !== code || queuedUnit.members.length !== members.length
        || members.some(id=>queueOf.get(id)!==queuedUnit))) {
      cancelQueuedFor(members);
      return sendJson(res,409,{ok:false,error:'Your party changed. Please start the search again.'});
    }
    if (!queuedUnit && !enqueue(members, code, Date.now())) {
      return sendJson(res,409,{ok:false,error:'Somebody in your party is already searching or in a match.'});
    }
    const size = queuedPlayers();
    for (const id of members) {
      sendTo(id, { type: 'queued', position: queuePosition(id), size });
    }
    broadcast(stats());
    const match = tryFormMatch();
    sendJson(res, 200, { ok: true, position: queuePosition(account.player_id),
                         size, matched: Boolean(match), party: members.length > 1 ? members.length : 0 });
    } finally {
      if (queueIntents.get(account.player_id) === intent) queueIntents.delete(account.player_id);
    }
  }

  function rotatedPeers(rows, steamId, now) {
    if (rows.length < 2) return rows;
    const bucket = Math.floor(now / 30000);
    const digest = crypto.createHash('sha256').update(`${steamId}:${bucket}`).digest();
    const start = digest.readUInt32BE(0) % rows.length;
    return rows.slice(start).concat(rows.slice(0,start));
  }

  // Session markers identify one app generation. They are random, short-lived, and never IPs.
  async function handleNetwork(req,res,account,pathname,method) {
    const id = account.player_id;
    const now = Date.now();
    if (pathname === '/api/network/relay' && method === 'GET') {
      try { return sendJson(res,200,await credentialIssuer.issue(id)); }
      catch (err) {
        const status = err && (err.status === 429 || err.status === 503) ? err.status : 503;
        const error = status === 429 ? 'Too many relay credential requests.'
          : 'Relay credentials are temporarily unavailable.';
        return sendJson(res,status,{ok:false,error});
      }
    }
    if (pathname === '/api/network/peers' && method === 'GET') {
      const mine = networkRegistry.player(id);
      if (!networkLib.ready(mine,now)) return sendJson(res,409,{ok:false,error:'Refresh your connection profile.'});
      if (!isQueued(id)) return sendJson(res,200,{ok:true,queued:false,revision:mine.revision,
        next_offset:null,peers:[]});
      const active = [...networkRegistry.profiles.values()].filter(p => p.id !== id && bySteam.has(p.id)
        && isQueued(p.id) && !inMatch.has(p.id) && networkLib.ready(p,now)
        && networkLib.compatible([mine,p]));
      active.sort((a,b) => a.id.localeCompare(b.id));
      const rotated = rotatedPeers(active,id,now);
      const params = new URL(req.url || pathname,'http://localhost').searchParams;
      const offset = Math.max(0,Math.floor(Number(params.get('offset')) || 0));
      const rows = rotated.slice(offset,offset + networkLib.MAX_PEERS);
      return sendJson(res,200,{ok:true,queued:true,revision:mine.revision,
        next_offset:offset + rows.length < rotated.length ? offset + rows.length : null,
        peers:rows.map(p => ({player_id:p.id,location:p.location,revision:p.revision}))});
    }
    if (method !== 'POST' || pathname === '/api/network/peers' || pathname === '/api/network/relay') {
      return sendJson(res,405,{ok:false,error:'Method not allowed.'});
    }
    let body;
    try { body = identity.parse((await readBody(req,131072)).toString('utf8')); }
    catch { return sendJson(res,400,{ok:false,error:'Invalid connection data.'}); }
    if (pathname === '/api/network/signal') {
      try { signalLimiter.take(id); }
      catch { return sendJson(res,429,{ok:false,error:'Too many relay requests.'}); }
    }
    if (pathname === '/api/network/pings') {
      try { reportLimiter.take(id); }
      catch { return sendJson(res,429,{ok:false,error:'Too many relay reports.'}); }
    }
    try {
      if (pathname === '/api/network/profile') {
        const currentMatch = matches.get(inMatch.get(id));
        const old = networkRegistry.preferences.get(id) || currentMatch?.network_preferences?.[id];
        if (body && body.unavailable === true) {
          networkRegistry.profiles.delete(id);
          networkRegistry.retirePlayer(id);
          const removed = removeFromQueue(id);
          if (removed) {
            const error = removed.members.length > 1
              ? 'A party member has no fresh connection measurements. Check Settings before searching again.'
              : 'Connection measurements are unavailable. Check Settings before searching again.';
            for (const member of removed.members) sendTo(member,{type:'unqueued',network_unready:true,error});
            broadcast(stats());
          }
          return sendJson(res,200,{ok:true,ready:false});
        }
        const changed = old && (old.region !== body?.region || old.cross_region !== (body?.cross_region === true));
        if (changed && inMatch.has(id)) return sendJson(res,409,{ok:false,error:'Finish your match before changing regions.'});
        const oldRevision = networkRegistry.player(id)?.revision;
        const profile = networkRegistry.profile(id,body,now);
        if (changed || (oldRevision && oldRevision !== profile.revision)) {
          const removed = removeFromQueue(id);
          if (removed) {
            for (const member of removed.members) sendTo(member,{type:'unqueued'});
            broadcast(stats());
          }
        }
        return sendJson(res,200,{ok:true,ready:true,player_id:id,transport:networkLib.TRANSPORT,
          region:profile.region,revision:profile.revision,
          target_ping_ms:networkLib.TARGET_PING,max_ping_ms:networkLib.MAX_PING});
      }
      if (pathname === '/api/network/signal') {
        const data = relayLib.validateSignal(body?.type,body?.data);
        const mine = networkRegistry.player(id);
        const peer = networkRegistry.player(body?.peer);
        if (!networkLib.ready(mine,now) || mine.revision !== body?.revision
            || !peer || peer.revision !== body?.peer_revision
            || !isQueued(id) || !isQueued(body.peer) || !bySteam.has(id) || !bySteam.has(body.peer)
            || inMatch.has(id) || inMatch.has(body.peer)
            || !networkLib.compatible([mine,peer])) {
          return sendJson(res,409,{ok:false,error:'Relay peer is no longer available.'});
        }
        let attempt;
        if (body.type === 'offer') {
          attempt = networkRegistry.authorizeAttempt(id,body.peer,mine.revision,peer.revision,
            body.attempt,now);
        } else {
          attempt = networkRegistry.attempt(body.attempt,now);
          const forward = attempt && attempt.offerer === id && attempt.answerer === body.peer
            && attempt.offererRevision === mine.revision && attempt.answererRevision === peer.revision;
          const reverse = attempt && attempt.answerer === id && attempt.offerer === body.peer
            && attempt.answererRevision === mine.revision && attempt.offererRevision === peer.revision;
          if (!attempt || (!forward && !reverse) || (body.type === 'answer' && !reverse)) {
            return sendJson(res,409,{ok:false,error:'Relay attempt is no longer authorized.'});
          }
          if (body.type === 'answer') attempt.answered = true;
        }
        sendTo(body.peer,{type:'network_signal',from:id,revision:mine.revision,
          target_revision:peer.revision,attempt:body.attempt,signal:{type:body.type,data}});
        return sendJson(res,200,{ok:true,expires_at:attempt.expires});
      }
      if (pathname !== '/api/network/pings') {
        return sendJson(res,404,{ok:false,error:'Not found.'});
      }
      if (!isQueued(id) || !bySteam.has(id) || !Array.isArray(body?.peers)
          || body.peers.some((r) => !isQueued(r?.player_id || r?.steam_id) || !bySteam.has(r?.player_id || r?.steam_id)
            || inMatch.has(r?.player_id || r?.steam_id))) {
        return sendJson(res,409,{ok:false,error:'Relay peer is no longer available.'});
      }
      networkRegistry.report(id,body,now);
      drainQueue();
      return sendJson(res,200,{ok:true});
    } catch (err) { return sendJson(res,400,{ok:false,error:err.message}); }
  }

  /**
   * Stop searching. ANY member may do this, not only the leader: a party whose leader has
   * wandered off mid-queue must not be a trap, and the cost of being wrong is one re-queue.
   * The whole unit comes out (removeFromQueue) and everybody in it is told.
   */
  function handleQueueLeave(res, account) {
    const pendingParty = parties.get(partyOf.get(account.player_id));
    for (const id of pendingParty ? pendingParty.members : [account.player_id]) queueIntents.delete(id);
    const unit = removeFromQueue(account.player_id);
    for (const id of (unit ? unit.members : [account.player_id])) {
      sendTo(id, { type: 'unqueued' });
    }
    broadcast(stats());
    sendJson(res, 200, { ok: true, was_queued: Boolean(unit) });
  }

  /**
   * Leave a match you are in.
   *
   *   found      -> a decline: the match dies and you are the one blamed for it.
   *   connecting -> a no-show by hand. You said you were coming and then walked out while
   *                 nine other people were loading in, so it costs exactly what timing out
   *                 costs. Nobody else pays.
   *   ready      -> still an ABANDON once real matches exist, and where the penalty ladder
   *                 will hang (memory section 2, C6-C9). Deliberately unpriced for now, so it
   *                 is recorded here rather than silently forgotten.
   */
  async function handleReport(req, res, account) {
    let body = {};
    try {
      // Python JSON escapes non-ASCII; allow the full 1,000-character explanation.
      const raw = await readBody(req, 16384);
      if (raw && raw.length) body = identity.parse(raw.toString('utf8'));
    } catch { body = {}; }
    const result = await reportPlayer(account.player_id, body);
    sendJson(res, result.ok ? 200 : 409, result);
  }

  /**
   * File a bug against the hub. The body is one field; who it is from is the bearer token.
   *
   * 429 for the cooldown and 400 for everything else, because the hub has to tell those two
   * apart: one of them is worth trying again in a moment and the other never is.
   */
  async function handleBugReport(req, res, account) {
    let body = {};
    try {
      // A bigger cap than the 4096 every other body gets: this one is prose a person typed, and
      // BUG_TEXT_MAX is 2000 characters that may each be four bytes of UTF-8.
      const raw = await readBody(req, 16384);
      if (raw && raw.length) body = identity.parse(raw.toString('utf8'));
    } catch { body = {}; }
    const result = await submitBugReport(account.player_id, body);
    sendJson(res, result.ok ? 200 : (result.error === 'too_fast' ? 429 : 400), result);
  }

  async function handleMatchLeave(res, account) {
    const matchId = inMatch.get(account.player_id);
    if(matches.get(matchId)?.state==='live')return matchOperation(matchId,async()=>{
      const match=matches.get(matchId);
      if(!match||match.state!=='live'||match.final_snapshot)return sendJson(res,409,{ok:false,error:'Match is finishing.'});
      const before=match.reconnect;
      const since=Date.now();
      match.reconnect={...(before||{}),[account.player_id]:before?.[account.player_id]||{since,deadline:since+300000}};
      try { await persistLive(match); }
      catch { match.reconnect=before; return sendJson(res,503,{ok:false,error:'Reconnect window could not be saved.'}); }
      if(matches.get(matchId)!==match || match.state!=='live' || match.final_snapshot)
        return sendJson(res,409,{ok:false,error:'Match is finishing.'});
      revokeJoinPermit(account.player_id);
      const waiting=Object.entries(match.reconnect).map(([player_id,w])=>({player_id,deadline:w.deadline}));
      for(const p of match.players)sendTo(p.player_id,{type:'match_reconnect',match_id:matchId,waiting});
      return sendJson(res,200,{ok:true,was_in_match:true,reconnect:true,deadline:match.reconnect[account.player_id].deadline});
    });
    const match = matchId && matches.get(matchId);
    if (!match) return sendJson(res, 200, { ok: true, was_in_match: false });
    if (match.final_snapshot) return sendJson(res, 409, { ok: false, error: 'Final match data is being saved.' });

    if (match.state === 'found') {
      closeMatch(match, 'declined', [account.player_id]);
      return sendJson(res, 200, { ok: true, was_in_match: true, declined: true });
    }

    if (match.state === 'connecting') {
      const me = match.players.find((p) => p.player_id === account.player_id);
      if (me && !me.connected) {
        const punished = { [account.player_id]: applyNoShow(account.player_id) };
        closeMatch(match, 'no_show', [account.player_id], punished);
        return sendJson(res, 200, { ok: true, was_in_match: true, no_show: true,
                                    penalty: punished[account.player_id] });
      }
    }

    if ((match.host || (match.network && match.network.host)) === account.player_id
        && ['ready','connecting','live'].includes(match.state)) {
      closeMatch(match,match.state === 'live' ? 'abandoned' : 'declined',[account.player_id]);
      return sendJson(res,200,{ok:true,was_in_match:true,declined:true});
    }

    // Walking out of a match that CARRIES ON without you: the lobby, or the connect window
    // after you had already reported in. Unpriced for now (the ladder in C6-C9 is not built),
    // and deliberately NOT archived here - the match is not over, and writing a record now
    // would both freeze it before the real ending and, through the `archived` guard, stop the
    // match these nine go on to play from ever being recorded at all. Instead the leaver is
    // remembered on the match, so they appear on the record whenever it is finally written,
    // marked as having left. That is what the abandon ladder will read.
    revokeJoinPermit(account.player_id);
    const me = match.players.find((p) => p.player_id === account.player_id);
    if (me) {
      match.left = match.left || [];
      match.left.push({ ...me, at: Date.now(), left_state: match.state });
    }
    inMatch.delete(account.player_id);
    match.players = match.players.filter((p) => p.player_id !== account.player_id);
    for (const p of match.players) {
      sendTo(p.player_id, { type: 'match_left', match_id: match.id, player_id: account.player_id,
                           remaining: match.players.length });
    }
    if (!match.players.length) {
      // The last one out: now it really is over, and nobody is left to play it.
      if (match.timer) clearTimeout(match.timer);
      matches.delete(match.id);
      forgetMatch(match.id);
      archiveMatch(match, { outcome: 'cancelled', reason: 'abandoned' });
    }
    sendTo(account.player_id, { type: 'match_over', match_id: match.id });
    broadcast(stats());
    return sendJson(res, 200, { ok: true, was_in_match: true, declined: false });
  }

  function handleAccept(res, account) {
    const result = acceptMatch(account.player_id);
    sendJson(res, result.ok ? 200 : 409, result);
  }

  async function handleConnecting(req, res, account) {
    let body = {};
    try {
      const raw = await readBody(req, 4096);
      if (raw && raw.length) body = identity.parse(raw.toString('utf8'));
    } catch { body = {}; }
    const result = beginConnect(account.player_id, body);
    sendJson(res, result.ok ? 200 : 409, result);
  }

  function handleConnected(res, account) {
    const result = reportConnected(account.player_id);
    sendJson(res, result.ok ? 200 : 409, result);
  }

  async function readJsonBody(req) {
    try {
      const raw = await readBody(req, 4096);
      if (raw && raw.length) {
        const parsed = identity.parse(raw.toString('utf8'));
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
      }
    } catch { /* a bad body is an empty one */ }
    return {};
  }

  async function handleCoin(req, res, account) {
    const result = flipCoin(account.player_id, await readJsonBody(req));
    sendJson(res, result.ok ? 200 : 409, result);
  }

  async function handleChoose(req, res, account) {
    const result = chooseAdvantage(account.player_id, await readJsonBody(req));
    sendJson(res, result.ok ? 200 : 409, result);
  }

  async function handleSide(req, res, account) {
    const result = pickSide(account.player_id, await readJsonBody(req));
    sendJson(res, result.ok ? 200 : 409, result);
  }

  async function handleBan(req, res, account) {
    const result = banMap(account.player_id, await readJsonBody(req));
    sendJson(res, result.ok ? 200 : 409, result);
  }

  async function handleChat(req, res, account) {
    const result = sendChat(account.player_id, await readJsonBody(req));
    sendJson(res, result.ok ? 200 : 409, result);
  }

  /**
   * One match's chat transcript, for a moderator looking at a report.
   *
   * Admins only, and 403 rather than 404 for a non-admin: somebody whose id was mistyped needs to
   * see a refusal, not a page that looks like it does not exist (same rule as adminOverview).
   * `personas` is joined in from the match record so the log reads as names rather than ids - the
   * call signs are a LOBBY affordance for players, not a way to hide who said what from staff.
   */
  async function adminChat(steamId, matchId) {
    if (!isAdmin(steamId)) return { ok: false, error: 'not an admin' };
    const id = String(matchId || '').slice(0, 64);
    if (!id) return { ok: false, error: 'which match?' };
    const live = matches.get(id);
    const lines = live && live.chat ? live.chat.slice(-CHAT_KEEP) : ((await readChat(id)) || {}).lines;
    if (!lines) return { ok: false, error: 'no transcript for that match', match_id: id, expired: true };
    const personas = {};
    const record = archived.get(id) || null;
    for (const p of (live ? live.players : (record && record.players) || [])) {
      personas[p.player_id] = p.persona || '';
    }
    return { ok: true, match_id: id, kept_seconds: CHAT_TTL_SECONDS, lines, personas };
  }

  function handleStats(res) {
    sendJson(res, 200, { ok: true, ...stats() });
  }

  /**
   * The Match History sub-tab.
   *
   *   GET /api/match/history        the caller's own last HISTORY_KEEP matches, newest first
   *   GET /api/match/history?id=X   the full record for one of them, for the detail pop-up
   *
   * A player may only read matches they were in - readMatch checks the roster - so a guessed
   * match id gives back the same 404 as one that never existed.
   */
  async function handleHistory(res, account, query) {
    if (query === null) {
      // The query string could not be read at all. Answering with the whole list would turn a
      // detail fetch into a list fetch and look like a success, so say what actually happened.
      return sendJson(res, 400, { ok: false, error: 'Could not read the request.' });
    }
    const wanted = (query && query.get('id')) || '';
    if (wanted) {
      const full = await readMatch(String(wanted).slice(0, 64), account.player_id);
      if (!full) return sendJson(res, 404, { ok: false, error: 'No such match.' });
      return sendJson(res, 200, { ok: true, match: full });
    }
    const matches_ = await readHistory(account.player_id);
    sendJson(res, 200, { ok: true, matches: matches_, keep: HISTORY_KEEP });
  }

  /** Returns true when it handled the request.
   *
   * `url` is optional: server.cjs has already parsed one, so it hands it over rather than
   * paying for a second parse, but the route works without it (test_wire.py calls it with
   * four arguments) by parsing req.url itself. */
  async function route(req, res, method, pathname, url) {
    if(activityGate && owns(pathname) && pathname !== '/api/live/stats')
      return activityGate.read(()=>routeAdmitted(req,res,method,pathname,url));
    return routeAdmitted(req,res,method,pathname,url);
  }
  async function routeAdmitted(req, res, method, pathname, url) {
    if (pathname === '/api/live/stats' && (method === 'GET' || method === 'HEAD')) {
      handleStats(res);
      return true;
    }
    if (!NEEDS_AUTH.includes(pathname)) return false;

    // THE FIRST REQUESTS AFTER A REDEPLOY ARE THE ONES THAT MATTER. Ten hubs whose stream just
    // died reconnect within a second or two of the new container answering, and if they arrive
    // before the matches have been read back they are told they are in no match - which is the
    // exact bug this recovery exists to fix, just with a smaller window. So every authenticated
    // route waits for it. It resolves immediately on any later request, and at once on a service
    // with no store at all.
    await ready;
    try { await ensureRecovery(); }
    catch {
      sendJson(res, 503, { ok: false, error: 'Match recovery is temporarily unavailable. Try again shortly.' });
      return true;
    }

    const verified = await whoami(bearer(req));
    const account = verified && {...verified, player_id: identity.playerOf(verified), game_steam_id: identity.gameOf(verified)};
    if (!account || !identity.validPlayer(account.player_id) || !identity.validSteam(account.game_steam_id)) {
      sendJson(res, 401, { ok: false, error: 'Not signed in.' });
      return true;
    }
    const prior = verifiedPlayers.get(account.player_id);
    const frozen = identity.gameFor(matches.get(inMatch.get(account.player_id)), account.player_id);
    if ((frozen && frozen !== account.game_steam_id) ||
        (queueOf.has(account.player_id) && prior && prior.game_steam_id !== account.game_steam_id)) {
      sendJson(res,409,{ok:false,error:'Finish your match or leave the queue before changing game accounts.'}); return true;
    }
    verifiedPlayers.set(account.player_id, account);
    if(pathname.startsWith('/api/admin/')&&(account.auth_method!=='steam' || !isAdmin(account.steam_id))) {
      sendJson(res,403,{ok:false,error:'Steam administrator sign-in is required.'});
      return true;
    }
    req.analyticsActor = account.player_id;
    req.analyticsMatch = inMatch.get(account.player_id) || null;
    // The same choke point records what the caller is running. Every authenticated route, the
    // stream included - which is what makes a party member's version knowable without asking
    // them for it. It only RECORDS; the one place that refuses anything is handleQueueJoin.
    noteVersions(req, account.player_id);
    // ONE CHOKE POINT. Every competitive route is behind this check, including the stream, so a
    // banned account cannot queue, accept, report, add friends or watch. Official support remains
    // accessible so a banned entrant can appeal and read an administrator response. There is no
    // second place to forget. 403 with the reason, because a player who does not know they are
    // banned files a bug against the hub instead.
    try { await Promise.all([...new Set([account.player_id,account.game_steam_id])].map(loadBan)); }
    catch { sendJson(res,503,{ok:false,error:'Account status is temporarily unavailable. Try again shortly.'});return true; }
    const ban = banOf(account.player_id) || banOf(account.game_steam_id);
    const supportAccess=['/api/tournament','/api/tournament/support','/api/messages','/api/messages/thread','/api/messages/send','/api/messages/read'].includes(pathname)
      || Boolean(competitionGuard && (pathname==='/api/live'||/^\/api\/(friends|party)(\/|$)/.test(pathname)));
    if (ban && !supportAccess) {
      sendJson(res, 403, { ok: false, error: 'banned', reason: ban.reason || '',
                           until: ban.until || 0 });
      return true;
    }
    // Opening the stream writes the durable profile directory as well as
    // connection state, so it needs the same ownership claim as player actions.
    if(admitGameplay && !(ban && supportAccess) && (pathname==='/api/live' || method==='POST') && !pathname.startsWith('/api/network/') && !pathname.startsWith('/api/admin/')) {
      try {
        if(!await admitGameplay(bearer(req),account)) {
          sendJson(res,401,{ok:false,error:'Your account changed. Sign in again.'});return true;
        }
      } catch {sendJson(res,503,{ok:false,error:'Account verification is temporarily unavailable.'});return true;}
    }
    if(pathname==='/api/match/concede'&&method==='POST'){
      const result=await concedeMatch(account,await readJsonBody(req));
      sendJson(res,result.ok?200:result.unavailable?503:409,result);return true;
    }
    if(pathname.startsWith('/api/messages')) {
      res.setHeader('cache-control','no-store, private');
      const read=pathname==='/api/messages'||pathname==='/api/messages/thread';
      if(method!==(read?'GET':'POST')){sendJson(res,405,{ok:false,error:'method_not_allowed'});return true;}
      try {
        if(pathname==='/api/messages')await tournament?.flushNotifications?.();
        let result;
        if(pathname==='/api/messages'){
          result=await messages.inbox(account.player_id);
          if(ban&&!competitionGuard&&result.ok){result.threads=result.threads.filter(t=>t.target==='admin');result.unread=result.threads.reduce((n,t)=>n+t.unread,0);}
        }else if(pathname==='/api/messages/thread'){
          if(ban&&!competitionGuard&&url.searchParams.get('target')!=='admin'){sendJson(res,403,{ok:false,error:'banned'});return true;}
          result=await messages.thread(account.player_id,url.searchParams.get('target')||'',url.searchParams.get('before'));
        }
        else {const body=await readJsonBody(req);
          if(ban&&!competitionGuard&&body.target!=='admin'){sendJson(res,403,{ok:false,error:'banned'});return true;}
          if(pathname.endsWith('/send'))result=await messages.send(account.player_id,body);
          else if(pathname.endsWith('/read'))result=await messages.markRead(account.player_id,body.target,body.through_seq);
          else {result=await messages.block(account.player_id,body.target,body.blocked!==false);if(result.ok&&body.blocked!==false){for(const map of [friends,reqIn,reqOut]){setOf(map,account.player_id).delete(body.target);setOf(map,body.target).delete(account.player_id);}friendsChanged(account.player_id,body.target);}}
        }
        sendJson(res,result.ok?200:409,result);
      }catch{sendJson(res,503,{ok:false,error:'unavailable'});}return true;
    }
    if (pathname === '/api/tournament' || pathname === '/api/tournament/register' || pathname === '/api/tournament/support') {
      res.setHeader('cache-control','no-store, private');
      if(method!==(pathname==='/api/tournament'?'GET':'POST')){sendJson(res,405,{ok:false,error:'method_not_allowed'});return true;}
      try {
        if(!tournament)throw Error('Event unavailable');
        // Body identity and score are deliberately ignored: verified account and receipts only.
        const result=pathname.endsWith('/support')?await tournament.support(account.player_id,await readJsonBody(req)):pathname.endsWith('/register')?await tournament.register(account):await tournament.view(account.player_id);
        sendJson(res,result.ok?200:409,result);
      } catch {sendJson(res,503,{ok:false,error:'unavailable'});}
      return true;
    }
    if (pathname === '/api/live' && method === 'GET') { await handleStream(req, res, account); return true; }
    if (pathname.startsWith('/api/network/')) { await handleNetwork(req,res,account,pathname,method); return true; }
    if (pathname === '/api/queue/join' && method === 'POST') { await handleQueueJoin(res, account, bearer(req)); return true; }
    if (pathname === '/api/queue/leave' && method === 'POST') { handleQueueLeave(res, account); return true; }
    if (pathname === '/api/match/combat-warning' && method === 'POST') {
      try { const body = identity.parse((await readBody(req, 1024)).toString('utf8'));
        const result = await acknowledgeCombatWarning(account.player_id, body);
        sendJson(res, result.ok ? 200 : 409, result);
      } catch { sendJson(res, 503, { ok: false, error: 'Warning receipt unavailable.' }); }
      return true;
    }
    if (pathname === '/api/match/accept' && method === 'POST') { handleAccept(res, account); return true; }
    if (pathname === '/api/match/recovery' && method === 'POST') {
      res.setHeader('cache-control','no-store, private');
      try { const result=await recoveryAction(account.player_id,await readJsonBody(req));
        sendJson(res,result.ok?200:409,result);
      } catch {sendJson(res,503,{ok:false,error:'Recovery is temporarily unavailable.'});}
      return true;
    }
    if (pathname === '/api/match/void-vote' && method === 'POST') {
      const result = await voteToVoid(account, await readJsonBody(req));
      sendJson(res, result.ok ? 200 : result.unavailable ? 503 : 409, result);
      return true;
    }
    if (pathname === '/api/match/leave' && method === 'POST') { await handleMatchLeave(res, account); return true; }
    if (pathname === '/api/report' && method === 'POST') { await handleReport(req, res, account); return true; }
    if (pathname === '/api/bug' && method === 'POST') { await handleBugReport(req, res, account); return true; }
    if (pathname === '/api/admin/chat' && (method === 'GET' || method === 'HEAD')) {
      const result = await adminChat(account.steam_id, url && url.searchParams.get('match'));
      sendJson(res, result.ok ? 200 : (result.error === 'not an admin' ? 403 : 404), result);
      return true;
    }
    if (pathname === '/api/admin/overview' && (method === 'GET' || method === 'HEAD')) {
      const result = await adminOverview(account.steam_id,
                                         { limit: url && url.searchParams.get('limit') });
      // 403 and not 404: an admin whose id was mistyped needs to see a refusal, not a page that
      // looks like it does not exist.
      sendJson(res, result.ok ? 200 : 403, result);
      return true;
    }
    if ((pathname === '/api/admin/ban' || pathname === '/api/admin/unban') && method === 'POST') {
      let body = {};
      try {
        const raw = await readBody(req, 4096);
        if (raw && raw.length) body = identity.parse(raw.toString('utf8'));
      } catch { body = {}; }
      const fn = pathname.endsWith('/ban') ? banAccount : unbanAccount;
      const result = await fn(account.steam_id, body);
      sendJson(res, result.ok ? 200 : 403, result);
      return true;
    }
    if (pathname === '/api/admin/player' && (method === 'GET' || method === 'HEAD')) {
      if (!isAdmin(account.steam_id)) { sendJson(res, 403, { ok: false, error: 'not an admin' }); return true; }
      const who = (url && url.searchParams.get('player_id')) || '';
      sendJson(res, 200, await reportsFor(who));
      return true;
    }
    if (pathname === '/api/friends/list' && (method === 'GET' || method === 'HEAD')) {
      sendJson(res, 200, await friendList(account.player_id));
      return true;
    }
    if (pathname === '/api/friends/code' && method === 'POST') {
      sendJson(res, 200, { ok: true, code: await refreshFriendCode(account.player_id) });
      return true;
    }
    if (pathname.startsWith('/api/friends/') && method === 'POST') {
      const verb = pathname.slice('/api/friends/'.length);
      const fn = { request: requestFriend, accept: acceptFriend, decline: declineFriend,
                   cancel: cancelFriendRequest, remove: removeFriend }[verb];
      if (!fn) return false;
      let body = {};
      try {
        const raw = await readBody(req, 4096);
        if (raw && raw.length) body = identity.parse(raw.toString('utf8'));
      } catch { body = {}; }
      const result = await fn(account.player_id, body);
      sendJson(res, result.ok ? 200 : 409, result);
      return true;
    }
    if (pathname === '/api/leaderboard' && (method === 'GET' || method === 'HEAD')) {
      const result = await leaderboard(account.player_id, url && url.searchParams.get('limit'));
      sendJson(res, 200, result);
      return true;
    }
    if (pathname === '/api/match/coin' && method === 'POST') { await handleCoin(req, res, account); return true; }
    if (pathname === '/api/match/choose' && method === 'POST') { await handleChoose(req, res, account); return true; }
    if (pathname === '/api/match/side' && method === 'POST') { await handleSide(req, res, account); return true; }
    if (pathname === '/api/match/ban' && method === 'POST') { await handleBan(req, res, account); return true; }
    if (pathname === '/api/match/chat' && method === 'POST') { await handleChat(req, res, account); return true; }
    if (pathname === '/api/match/connecting' && method === 'POST') { await handleConnecting(req, res, account); return true; }
    if (pathname === '/api/match/connected' && method === 'POST') { handleConnected(res, account); return true; }
    if (pathname === '/api/party/create' && method === 'POST') { handlePartyCreate(res, account); return true; }
    if (pathname === '/api/party/join' && method === 'POST') { await handlePartyJoin(req, res, account); return true; }
    if (pathname === '/api/party/leave' && method === 'POST') { handlePartyLeave(res, account); return true; }
    if (pathname === '/api/party/refresh-code' && method === 'POST') { handlePartyRefresh(res, account); return true; }
    if (pathname.startsWith('/api/party/invite') && method === 'POST') {
      let body = {};
      try {
        const raw = await readBody(req, 1024);
        if (raw && raw.length) body = identity.parse(raw.toString('utf8'));
      } catch { body = {}; }
      let result;
      if (pathname === '/api/party/invite') result = await inviteToParty(account.player_id, body);
      else if (pathname === '/api/party/invite/accept') result = acceptPartyInvite(account.player_id, body);
      else if (pathname === '/api/party/invite/decline') result = declinePartyInvite(account.player_id, body);
      else return false;
      // 409 for every refusal, as the friends verbs do: these are all "the world says no"
      // (not in a party, not a friend, party full, invite gone), never a malformed request.
      sendJson(res, result.ok ? 200 : 409, result);
      return true;
    }
    if (pathname === '/api/match/completion' && method === 'GET') {
      const result = await completion(account.player_id, url?.searchParams.get('id'));
      sendJson(res, result.ok ? 200 : 409, result); return true;
    }
    if (pathname === '/api/match/history' && (method === 'GET' || method === 'HEAD')) {
      let query = (url && url.searchParams) || undefined;
      if (query === undefined) {
        // null, not undefined: handleHistory has to tell "no id was asked for" apart from
        // "the query string could not be read", or a broken URL silently returns everything.
        try { query = new URL(req.url, 'http://internal').searchParams; } catch { query = null; }
      }
      await handleHistory(res, account, query);
      return true;
    }
    sendJson(res, 405, { ok: false, error: 'Wrong method.' });
    return true;
  }

  /**
   * Put down everything this process is holding.
   *
   * Close report admission and drain every admitted batch/final before the last flush.
   * Railway calls this on redeploy; a lane may not have reached its persistence or
   * rating locks yet, so awaiting only those write maps can miss accepted work.
   */
  async function shutdown() {
    stopping = true;
    clearInterval(matchTick);
    clearInterval(restitutionTick);
    await Promise.allSettled([...matchOperations.values()]);
    try { await flushMatches(); } catch { /* the pending state remains retryable */ }
    await Promise.allSettled([...matchWriting.values(), ...ratingWriting.values(), ...penaltyWriting.values(), ...ratingOperations.values(),
      ...[...matches.values()].map(m => m.settling).filter(Boolean)]);
    // The writes inside flushMatches are fire-and-forget; give them a moment to leave rather
    // than tearing the process down on top of them.
    networkRegistry.attempts.clear();
    for (const match of matches.values()) {
      for (const key of ['timer','collectTimer','stage_timer','ban_timer']) if (match[key]) clearTimeout(match[key]);
    }
    for (const timer of partyGrace.values()) clearTimeout(timer);
    for (const box of partyInvites.values()) for (const invite of box.values()) clearTimeout(invite.timer);
    for (const clientId of [...clients.keys()]) drop(clientId);
  }

  async function prepareOwnership(ids) {
    await ensureRecovery();
    if(ids.some(id=>inMatch.has(id))) {
      const {AccountError}=require('./account-security.cjs');
      throw new AccountError(409,'active_match','Finish your match before changing account links.');
    }
  }
  function ownershipChanged({ids,accountId,steamId}) {
    cancelQueuedFor(ids);
    for(const id of ids) {queueIntents.delete(id);verifiedPlayers.delete(id);}
    for(const [key,client] of [...clients]) {
      if(ids.includes(client.steamId) || client.accountId===accountId ||
        (client.authMethod==='steam'&&client.sourceSteamId===steamId)) {
        try {client.res.end();}catch {}
        drop(key);
      }
    }
  }
  function nativePlayer(game, matchId) {
    if (!identity.validSteam(game)) return '';
    const candidates = matchId ? [matches.get(matchId)].filter(Boolean) : [...matches.values()];
    const found = candidates.flatMap(m => {
      const player = identity.playerFor(m,game);
      return player && inMatch.get(player) === m.id && m.players.some(p=>p.player_id===player) ? [player] : [];
    });
    return found.length === 1 ? found[0] : '';
  }
  const nativeHost = fn => (game, ...args) => {
    const player = nativePlayer(game);
    if (!player) return {ok:false,error:'unverified game identity'};
    return fn(player,...args);
  };
  const nativePermit = fn => (game, ...args) => {
    const player=nativePlayer(game); return player ? fn(player,...args) : false;
  };
  function nativeTeamRuling(game, subject, ask) {
    if (!identity.validSteam(subject)) return {ok:false,error:'invalid game identity'};
    const host=nativePlayer(game), match=matches.get(inMatch.get(host));
    if (!match || match.host!==host || !['connecting','live'].includes(match.state)) return {ok:false,error:'no match'};
    const player=identity.playerFor(match,subject);
    if (!player) return ['member','stranger'].includes(ask)
      ? {ok:true,match_id:match.id,subject,ask,member:false,yes:ask==='stranger'}
      : {ok:false,error:'not on the roster'};
    return {...teamRuling(host,player,ask),subject};
  }
  function nativeStartReady(game, id, rows) {
    const host=nativePlayer(game,id), match=matches.get(id);
    if (!host || !Array.isArray(rows)) return {ok:false,error:'invalid roster'};
    if(soloMatch(match)) {
      rows=require('./private-solo.cjs').humans(rows,privateSoloSteam);
      if(!rows)return {ok:false,error:'private test requires the assigned human and nine active bots'};
    }
    return startReady(host,id,rows.map(row=>({...row,player_id:identity.playerFor(match,row?.steam_id)})));
  }
  function nativePresence(game,id,rows) {
    const host=nativePlayer(game,id), match=matches.get(id);
    if(!host||!Array.isArray(rows)||rows.length>10)return {ok:false,error:'invalid presence roster'};
    const present=[];
    for(const row of rows) {
      if(!row?.steam_id) { if(soloMatch(match))continue; return {ok:false,error:'invalid presence identity'}; }
      const player=identity.playerFor(match,row.steam_id);
      if(!player||![0,1].includes(row.active))return {ok:false,error:'invalid presence identity'};
      if(row.active===1)present.push(player);
    }
    return matchPresence(host,id,present);
  }
  function nativeTeam(game, report) {
    const host=nativePlayer(game), match=matches.get(inMatch.get(host));
    return gameReportedTeam(host,{...report,subject:identity.playerFor(match,report?.subject)});
  }
  function nativeTeamKill(game, report) {
    const host=nativePlayer(game), match=matches.get(inMatch.get(host));
    return teamKillReported(host,{...report,killer:identity.playerFor(match,report?.killer),victim:identity.playerFor(match,report?.victim)});
  }
  async function nativeFinal(game, fields) {
    if (!identity.validSteam(game)) return {ok:false,error:'invalid identity'};
    const id=fields?.match_id;
    let player=nativePlayer(game,id);
    if (!player) {
      const receipt=await readReceipt(id);
      const full=receipt?.publicMatch;
      if (full) {identity.freezeMatch(full); player=identity.playerFor(full,game);}
      if (!player || receipt.host!==player) return {ok:false,error:'no match'};
    }
    return finalSnapshot(player,fields);
  }
  function combatSummary(match, player, rounds) {
    return identity.combatSummary(match, combatLib.summaryAcross([...(match.combat_segments||[]),match.combatState], identity.gameFor(match,player), rounds));
  }

  const publicResult = fn => (...args) => {
    const result=fn(...args);
    return result?.then ? result.then(identity.wire) : identity.wire(result);
  };
  return {
    mode: mode.id,
    activity:()=>[...new Set([...queueOf.keys(),...inMatch.keys()])].map(id=>({player_id:id,
      game_steam_id:identity.gameFor(matches.get(inMatch.get(id)),id)||gameOfPlayer(id)})),
    async publicRank(id){const r=await loadRating(id);return {...progressLib.publicProgress(r,{top:isReaper(id,r.progress)}),
      matches:r.matches,wins:r.wins,losses:r.losses};},
    concedeMatch,
    correctCheaterMatches:receipt=>restitution.project(receipt),
    correctionJobs:()=>restitution.statuses(),
    route,
    broadcastStats:()=>broadcast(stats()),
    prepareOwnership, ownershipChanged,
    owns,
    shutdown,
    settleMatch,
    gameReportedIn: nativeHost(gameReportedIn),
    teamRuling: nativeTeamRuling, startReady: nativeStartReady, matchPresence: nativePresence, finalSnapshot: nativeFinal, completion, combatBatch: nativeHost(combatBatch),
    gameReportedTeam: nativeTeam,
    teamsAgree: publicResult(teamsAgree),
    teamKillReported: nativeTeamKill,
    gameReportedCombat: nativeHost(gameReportedCombat),
    acknowledgeCombatWarning,
    reportPlayer,
    reportsFor: publicResult(reportsFor),
    submitBugReport,
    bugReports: publicResult(bugReports),
    leaderboard: publicResult(leaderboard),
    adminOverview: publicResult(adminOverview),
    adminPlayers: publicResult(adminPlayers),
    adminAccountSummary,
    resetRank,
    setElo,
    setRank,
    isAdmin,
    async adminMessage(actor,body,context=null){
      if(!isAdmin(actor))return {ok:false,error:'not_an_admin'};
      if(!identity.validPlayer(body?.target))return {ok:false,error:'invalid_recipient'};
      const directory=await adminPlayers(actor,{player_id:body.target,limit:1,include_suspicion:false});
      if(!directory.rows?.some(r=>identity.playerOf(r)===body.target))return {ok:false,error:'unknown_player'};
      return messages.send(actor,body,{admin:true,context});
    },
    async adminMessages(actor,target,before){
      if(!isAdmin(actor))return {ok:false,error:'not_an_admin'};
      await tournament?.flushNotifications?.();
      return target?messages.adminThread(target,before):messages.adminInbox(before);
    },
    async adminMessagesRead(actor,body){
      if(!isAdmin(actor))return {ok:false,error:'not_an_admin'};
      return messages.adminRead(body.target,body.through_seq);
    },
    banAccount,
    unbanAccount,
    banOf,
    loadBan,
    friendList: publicResult(friendList),
    requestFriend,
    acceptFriend,
    declineFriend,
    cancelFriendRequest,
    removeFriend,
    friendCode,
    refreshFriendCode,
    inviteToParty,
    acceptPartyInvite,
    declinePartyInvite,
    gameReportedScore: nativeHost(gameReportedScore),
    authoriseReport,
    authoriseReportFresh, migrationReport, recoveryReport, recoveryAction, withReportAuthority,
    authoriseFinalReport,
    authoriseLegacyReport,
    gameReportedStats: nativeHost(gameReportedStats),
    gameReportedRound: nativeHost(gameReportedRound),
    gameReportedKill: nativeHost(gameReportedKill),
    grantHostPermit: nativePermit(grantHostPermit),
    takeHostPermit: nativePermit(takeHostPermit),
    noteHostLaunching: nativePermit(noteHostLaunching),
    revokeHostPermit: nativePermit(revokeHostPermit),
    grantJoinPermits,
    takeJoinPermit: nativePermit(takeJoinPermit),
    revokeJoinPermit: nativePermit(revokeJoinPermit),
    _internals: { networkRegistry, hostPermits,   // exposed for focused policy tests
                  clients, bySteam, queue, queueOf, matches, inMatch, penalties, history, archived,
                  parties, partyOf, partyGrace, partyInvites, ratings,
                  inviteToParty, acceptPartyInvite, declinePartyInvite, invitePayload,
                  joinPartyByCode, PARTY_INVITE_SECONDS, MAX_PARTY_INVITES,
                  readHistory, readMatch, archiveMatch, scoreboardOf,
                  expireLive,   // the stalled path, which keeps its scoreboard
                  ratingOf, loadRating, saveRating, mutateRating, settleMatch, drainRatingWrites, settlementKey, connectPayload,
                  gameReportedScore, gameReportedStats, gameReportedRound, gameReportedKill,
                  finishMatch, patchResult, DEFAULT_SCORE_LIMIT,
                  beginCollection, collectionComplete, COLLECT_SECONDS, collectFor,
                  gameReportedScore, finishMatch, patchResult, DEFAULT_SCORE_LIMIT,
                  gameReportedTeam, teamsAgree, goLiveIfReady, expireTeamsGate, startReady,
                  teamKillReported, gameReportedCombat, acknowledgeCombatWarning, combatHistories, TK_EARLY_SECONDS, TK_MATCH_LIMIT, tkEnforcing,
                  reportPlayer, reportsFor: publicResult(reportsFor), reports, REPORT_REASONS,
                  submitBugReport, bugReports: publicResult(bugReports), loadBugs, saveBugs, bugCooldown,
                  leaderboard: publicResult(leaderboard), personaOf, avatarOf, boardKey,
                  // The capstone cut: who is seated, and the read that decides it.
                  isReaper, refreshReaperCut, REAPER_AT, REAPER_SLOTS,
                  reaperState: () => ({ ...reaperCut, ids: [...reaperCut.ids] }),
                  // the remembered names: what makes an offline player a person and not a number
                  rememberProfile, loadProfiles, profiles,
                  adminOverview: publicResult(adminOverview), isAdmin, ADMIN_IDS,
                  resetRank, setElo, setRank, pushRating, ELO_CEILING,
                  adminPlayers: publicResult(adminPlayers), careers, careerOf, saveCareer, noteSeen, noteRating, noteReport,
                  creditMatch, loadRoster, rosterKey, directoryRow, statusOf,
                  PLAYER_COLUMNS, DIRECTORY_PAGE, DIRECTORY_MAX,
                  banAccount, unbanAccount, banOf, loadBan, bans,
                  friends, reqIn, reqOut, friendCodes, codeOwners,
                  MAX_FRIENDS, MAX_FRIEND_REQUESTS, ownerOfCode,
                  FRIEND_CODE_LEN, mintFriendCode, normaliseFriendCode,
                  TEAMS_GATE_SECONDS,
                  tryFormMatch, drainQueue, enqueue, removeFromQueue,
                  // The accept -> ready -> lobby -> connect path, exposed for the same reason the
                  // rest of this list is: it had NO direct coverage, which is how a match that one
                  // person can form got as far as production still un-acceptable.
                  acceptMatch, beginConnect, reportConnected, goLive,
                  armStageTurn, expireStageTurn, clearStageTurn, stageSeconds,
                  BAN_SECONDS, PICK_SECONDS, FLIP_SECONDS,
                  flipCoin, chooseAdvantage, pickSide, banMap, lobbyPayload, closeMatch,
                  queuedPlayers, queuePosition, buildLobby,
                  rating: ratingLib, matchmaker, valuation: valuationLib, progress: progressLib,
                  MATCH_TICK_MS,
                  versions, noteVersions, versionProblem, requireVersions,
                  VERSION_GATE, GATE_MODE, GATED_MODE_ID,
                  // surviving a redeploy
                  serialiseMatch, reviveMatch, flushMatches, forgetMatch, restoreMatches,checkHostTimeouts,
                  rearm, persisted, ready, ensureRecovery, LIVE_STATES, LIVE_STATE_TTL_SECONDS,
                  RECOVERY_GRACE_SECONDS,
                  liveMatchKey, liveIndexKey,
                  MATCH_SIZE, ACCEPT_SECONDS, LOBBY_SECONDS, CONNECT_SECONDS, LIVE_SECONDS,
                  MAX_PARTY, TEAM_SIZE, PARTY_GRACE_SECONDS,
                  NO_SHOW_BAN_SECONDS, NO_SHOW_RR, NO_SHOW_ELO: NO_SHOW_RR,
                  NO_SHOW_LADDER, NO_SHOW_DECAY_SECONDS,
                  applyNoShow, livePenalty, loadPenalty, expireConnect, queuePenaltiesPaused,
                  HISTORY_KEEP, HISTORY_TTL_SECONDS, MATCH_TTL_SECONDS },
  };
}

/**
 * The scoreline the HEARTBEAT carries, parsed into the fields gameReportedScore takes.
 *
 * `null` for anything else, which is most of them: the field is empty on every build older than
 * BB5 1.0.13, and an unparsable one must read as "this heartbeat is not a score report" rather
 * than as a zero-zero scoreline that would un-win a won match through the stale-report guard.
 *
 *   "<hostTeamId>|0:<score>|1:<score>;lim=<scoreLimit>"   ->   { scores, limit }
 *
 * It lives here, next to the function that consumes it, so the ONE test that matters - that the
 * exact string GM_BB5 builds survives the trip - can be written against the same code the server
 * runs (server.cjs is not requirable from the suite: it listens on import).
 */
const HEARTBEAT_SCORE_RE = /^(-?\d+)\|(-?\d+):(-?\d+)\|(-?\d+):(-?\d+);lim=(-?\d+)$/;
function parseHeartbeatScore(raw) {
  const m = HEARTBEAT_SCORE_RE.exec(String(raw == null ? '' : raw).trim());
  if (!m) return null;
  return { scores: `${m[1]}|${m[2]}:${m[3]}|${m[4]}:${m[5]}`, limit: m[6] };
}

module.exports = { create, owns, NEEDS_AUTH, versionOlder, GATED_MODE_ID, VERSION_GATE, GATE_MODE,
                   parseHeartbeatScore,
                   BUG_TEXT_MAX, BUG_KEEP, BUG_COOLDOWN_MS,
                   // The console renders these and validates presets against them, so it reads
                   // the one table rather than keeping a copy that drifts.
                   PLAYER_COLUMNS, DIRECTORY_PAGE, DIRECTORY_MAX, ELO_CEILING };
