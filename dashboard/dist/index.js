/**
 * Unraid dashboard tab.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React and
 * hooks, and talks to the plugin backend at /api/plugins/unraid/.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__ || {};
  var React = SDK.React;
  if (!React) {
    console.error("[unraid] Hermes plugin SDK not available");
    return;
  }
  var h = React.createElement;
  var hooks = SDK.hooks || SDK;
  var useState = hooks.useState;
  var useEffect = hooks.useEffect;
  var useCallback = hooks.useCallback;

  var BASE = "/api/plugins/unraid";

  // fetchJSON is the SDK's authenticated helper; fall back to plain fetch so
  // the tab still functions if the SDK surface changes.
  function api(path, options) {
    if (typeof SDK.fetchJSON === "function") return SDK.fetchJSON(BASE + path, options);
    var fetcher = typeof SDK.authedFetch === "function" ? SDK.authedFetch : window.fetch;
    return fetcher(BASE + path, options).then(function (r) { return r.json(); });
  }

  var S = {
    page: { padding: "24px", maxWidth: "860px", margin: "0 auto" },
    h1: { fontSize: "20px", fontWeight: 600, marginBottom: "4px" },
    sub: { fontSize: "13px", opacity: 0.7, marginBottom: "20px" },
    card: { border: "1px solid rgba(128,128,128,0.25)", borderRadius: "10px",
            padding: "16px", marginBottom: "16px" },
    cardTitle: { fontSize: "15px", fontWeight: 600, marginBottom: "2px" },
    cardHint: { fontSize: "12px", opacity: 0.65, marginBottom: "14px" },
    row: { display: "flex", alignItems: "center", justifyContent: "space-between",
           gap: "16px", padding: "8px 0" },
    label: { fontSize: "13px" },
    note: { fontSize: "11px", opacity: 0.6, marginTop: "2px" },
    input: { width: "150px", padding: "5px 8px", borderRadius: "6px",
             border: "1px solid rgba(128,128,128,0.35)", background: "transparent",
             color: "inherit", fontSize: "13px" },
    wide: { width: "100%", padding: "6px 8px", borderRadius: "6px",
            border: "1px solid rgba(128,128,128,0.35)", background: "transparent",
            color: "inherit", fontSize: "13px", fontFamily: "ui-monospace, monospace" },
    badge: { fontSize: "10px", padding: "1px 6px", borderRadius: "999px",
             border: "1px solid rgba(128,128,128,0.4)", opacity: 0.8, marginLeft: "8px" },
    btn: { padding: "7px 14px", borderRadius: "7px", fontSize: "13px", cursor: "pointer",
           border: "1px solid rgba(128,128,128,0.35)", background: "transparent",
           color: "inherit" },
    status: { fontSize: "12px", marginLeft: "12px", opacity: 0.8 }
  };

  // Where a value came from matters: "env" means editing here overrides an
  // environment variable, and clearing the field hands control back to it.
  function Source(props) {
    if (!props.from || props.from === "settings") return null;
    return h("span", { style: S.badge }, props.from === "env" ? "from env" : "default");
  }

  function Toggle(props) {
    return h("input", {
      type: "checkbox",
      checked: !!props.checked,
      onChange: function (e) { props.onChange(e.target.checked); },
      style: { width: "16px", height: "16px", cursor: "pointer" }
    });
  }

  function Row(props) {
    return h("div", { style: S.row },
      h("div", null,
        h("div", { style: S.label }, props.label, h(Source, { from: props.from })),
        props.note ? h("div", { style: S.note }, props.note) : null),
      h("div", null, props.children));
  }

  function App() {
    var st = useState(null); var settings = st[0]; var setSettings = st[1];
    var so = useState({}); var sources = so[0]; var setSources = so[1];
    var stt = useState(null); var status = stt[0]; var setStatus = stt[1];
    var ms = useState(""); var msg = ms[0]; var setMsg = ms[1];
    var bs = useState(false); var busy = bs[0]; var setBusy = bs[1];

    var load = useCallback(function () {
      api("/settings").then(function (d) {
        if (d && d.ok) { setSettings(d.settings); setSources(d.sources || {}); }
        else setMsg((d && d.error) || "failed to load settings");
      }).catch(function (e) { setMsg(String(e)); });
      api("/status").then(function (d) { if (d && d.ok) setStatus(d); }).catch(function () {});
    }, []);

    useEffect(function () { load(); }, [load]);

    function set(key, value) {
      setSettings(function (prev) {
        var next = {}; for (var k in prev) next[k] = prev[k];
        next[key] = value; return next;
      });
    }

    function save() {
      setBusy(true); setMsg("");
      var patch = {
        scopes: settings.scopes,
        protected_containers: settings.protected_containers,
        alerts_enabled: settings.alerts_enabled,
        min_importance: settings.min_importance,
        cooldown_seconds: settings.cooldown_seconds,
        max_per_hour: settings.max_per_hour,
        outbound_enabled: settings.outbound_enabled
      };
      api("/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch)
      }).then(function (d) {
        setBusy(false);
        if (d && d.ok) {
          setSettings(d.settings); setSources(d.sources || {});
          setMsg("Saved. Restart the gateway to apply alert changes.");
        } else setMsg((d && d.error) || "save failed");
      }).catch(function (e) { setBusy(false); setMsg(String(e)); });
    }

    if (!settings) {
      return h("div", { style: S.page }, h("div", { style: S.sub }, msg || "Loading..."));
    }

    var platform = status && status.platform_state;
    var perms = status && status.permissions;

    return h("div", { style: S.page },
      h("div", { style: S.h1 }, "Unraid"),
      h("div", { style: S.sub },
        "Alert forwarding, notification delivery, and API scopes. " +
        "Blank fields fall back to environment variables."),

      // ---- inbound ----
      h("div", { style: S.card },
        h("div", { style: S.cardTitle }, "Inbound alerts"),
        h("div", { style: S.cardHint },
          "Unraid warnings and alerts are pushed to the agent as they happen." +
          (platform ? "  Gateway state: " + (platform.state || "unknown") + "." : "")),
        h(Row, { label: "Forward alerts to the agent", from: sources.alerts_enabled,
                 note: "Requires NOTIFICATIONS:READ_ANY on the API key." },
          h(Toggle, { checked: settings.alerts_enabled,
                      onChange: function (v) { set("alerts_enabled", v); } })),
        h(Row, { label: "Minimum importance", from: sources.min_importance,
                 note: "Anything below this is ignored." },
          h("select", {
            value: settings.min_importance, style: S.input,
            onChange: function (e) { set("min_importance", e.target.value); }
          }, ["INFO", "WARNING", "ALERT"].map(function (o) {
            return h("option", { key: o, value: o }, o);
          }))),
        h(Row, { label: "Cooldown (seconds)", from: sources.cooldown_seconds,
                 note: "Per subject, so a flapping condition cannot repeat every few seconds." },
          h("input", { type: "number", min: 0, value: settings.cooldown_seconds, style: S.input,
                       onChange: function (e) { set("cooldown_seconds", e.target.value); } })),
        h(Row, { label: "Maximum per hour", from: sources.max_per_hour,
                 note: "Hard ceiling. Each forwarded alert can wake the agent." },
          h("input", { type: "number", min: 0, value: settings.max_per_hour, style: S.input,
                       onChange: function (e) { set("max_per_hour", e.target.value); } }))),

      // ---- outbound ----
      h("div", { style: S.card },
        h("div", { style: S.cardTitle }, "Outbound notifications"),
        h("div", { style: S.cardHint },
          "Agent messages delivered into Unraid's notification centre."),
        h(Row, { label: "Send notifications to Unraid", from: sources.outbound_enabled,
                 note: "Requires NOTIFICATIONS:CREATE_ANY on the API key." },
          h(Toggle, { checked: settings.outbound_enabled,
                      onChange: function (v) { set("outbound_enabled", v); } }))),

      // ---- scopes ----
      h("div", { style: S.card },
        h("div", { style: S.cardTitle }, "API scopes"),
        h("div", { style: S.cardHint },
          "RESOURCE:ACTION, comma separated. Wildcards allowed on either side. " +
          "The API key's own permissions are enforced by Unraid regardless."),
        h("div", { style: { padding: "4px 0" } },
          h("div", { style: S.label }, "Scopes", h(Source, { from: sources.scopes })),
          h("input", { type: "text", value: settings.scopes, style: S.wide,
                       placeholder: "*:READ_ANY,DOCKER:UPDATE_ANY",
                       onChange: function (e) { set("scopes", e.target.value); } })),
        h("div", { style: { padding: "10px 0 0" } },
          h("div", { style: S.label }, "Protected containers",
            h(Source, { from: sources.protected_containers })),
          h("div", { style: S.note },
            "Never updated, stopped or restarted. The agent's own container is detected " +
            "automatically; a sidecar sharing its network namespace must be named here."),
          h("input", { type: "text", value: settings.protected_containers, style: S.wide,
                       placeholder: "hermes,hermes-ts",
                       onChange: function (e) { set("protected_containers", e.target.value); } })),
        perms ? h("div", { style: { marginTop: "14px", fontSize: "12px", opacity: 0.75 } },
          (perms.tools_registered || []).length + " tools registered" +
          (perms.api_key_name ? "  |  key: " + perms.api_key_name : "") +
          ((perms.tools_likely_blocked_by_api_key || []).length
            ? "  |  " + perms.tools_likely_blocked_by_api_key.length + " in scope but not permitted by the key"
            : "")) : null),

      h("div", { style: { display: "flex", alignItems: "center" } },
        h("button", { style: S.btn, disabled: busy, onClick: save },
          busy ? "Saving..." : "Save"),
        h("button", { style: Object.assign({}, S.btn, { marginLeft: "8px" }), onClick: load },
          "Reload"),
        msg ? h("span", { style: S.status }, msg) : null));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("unraid", App);
  } else {
    console.error("[unraid] window.__HERMES_PLUGINS__.register unavailable");
  }
})();
