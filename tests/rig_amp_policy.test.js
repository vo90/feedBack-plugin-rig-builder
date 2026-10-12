const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'screen.js'), 'utf8');
const flush = async () => { for (let i = 0; i < 12; ++i) await Promise.resolve(); };
function deferred() { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; }

function load({enabled = false, applicable = true, service = true} = {}) {
    const timers = new Map(), intervals = new Map(), requests = [], effects = [];
    let nextTimer = 1, listener, snapshot = {ready: true, enabled, applicable, generation: 1};
    const policy = {version: 1, snapshot: () => snapshot,
        subscribe(fn) { listener = fn; fn(snapshot); return () => {}; }};
    const node = {classList: {add() {}, remove() {}}, addEventListener() {}, querySelector: () => null};
    const context = vm.createContext({
        console: {log() {}, warn() {}, error() {}},
        document: {getElementById: () => null, querySelectorAll: () => [],
            createElement: () => ({...node}), addEventListener() {},
            head: {appendChild() {}}, body: {appendChild() {}, contains: () => true}},
        localStorage: {getItem: () => null, setItem() {}},
        addEventListener() {}, dispatchEvent() {}, CustomEvent: class {},
        setTimeout(fn, ms) {const id = nextTimer++; timers.set(id, {fn, ms}); return id;},
        clearTimeout: id => timers.delete(id),
        setInterval(fn, ms) {const id = nextTimer++; intervals.set(id, {fn, ms}); return id;},
        clearInterval: id => intervals.delete(id), requestAnimationFrame: () => 0,
        fetch: async url => { requests.push(url); return {ok: true, json: async () => ({mega_chain_mode: true})}; },
        slopsmith: {on() {}}, feedBack: service ? {ampSims: policy} : {},
        feedBackDesktop: {audio: {
            async loadPreset() {effects.push('load'); return {success: true};},
            async clearChain() {effects.push('clear');}, async setGain() {effects.push('gain');},
            async startAudio() {effects.push('start');}, async isAudioRunning() {return true;},
        }},
    });
    context.window = context;
    // Plugin scripts run in a closure; window aliases must not overwrite their
    // function bindings (notably the diagnostic hotkey-prefetch wrapper).
    vm.runInContext(`(() => {${source}\nObject.assign(window, {
        rbAudioApi, rbReloadDefaultTone, rbLoadOverrideTone, rbLoadNativePresetPayload,
        __policyTest: {guard: RbAmpPolicy, mega: RbMegaChain}
    });})();`, context);
    const {guard, mega} = context.__policyTest;
    return {context, guard, mega, timers, intervals, requests, effects,
        async set(on) {snapshot = {...snapshot, enabled: on, generation: snapshot.generation + 1}; return listener(snapshot);}};
}

test('disabled boot and song/default/override/prefetch entries do no tone work', async () => {
    const s = load(); await flush();
    assert.equal(s.intervals.size, 0);
    const before = s.requests.length;
    assert.equal(await s.mega.buildForSong('song.feedpak'), false);
    assert.equal(await s.context.rbReloadDefaultTone(), false);
    assert.equal(await s.context.rbLoadOverrideTone(), false);
    await s.context.rbWarmHotkeyTones();
    assert.equal(s.requests.length, before);
    assert.deepEqual(s.effects, []);
});

test('off stops automatic loops and owned timers; re-enable creates each loop once', async () => {
    const s = load({enabled: true}); await flush();
    const initial = [...s.intervals.values()].map(x => x.ms).sort();
    assert.deepEqual(initial, [2000, 500]);
    let called = false;
    const id = s.guard.later(() => {called = true;}, 17);
    const stale = s.timers.get(id).fn;
    await s.set(false);
    assert.equal(s.intervals.size, 0);
    stale(); assert.equal(called, false);
    await s.set(true); await s.guard.resume();
    assert.deepEqual([...s.intervals.values()].map(x => x.ms).sort(), initial);
});

test('issued legacy load drains before suspension resolves, never clears another owner', async () => {
    const s = load({enabled: true}); await flush();
    const pending = deferred();
    s.context.feedBackDesktop.audio.loadPreset = () => pending.promise;
    const audio = s.context.rbAudioApi();
    const loading = audio.loadPreset('{}');
    const rejected = assert.rejects(loading, e => e.code === 'amp-sims-disabled');
    let done = false; const off = s.set(false).then(() => done = true);
    await flush(); assert.equal(done, false);
    pending.resolve({success: true}); await rejected; await off;
    assert.deepEqual(s.effects, []);
    await s.set(true);
    assert.equal((await audio.setGain('chain', 1)).code, 'amp-sims-disabled');
    assert.deepEqual(s.effects, []);
});

test('host suspension drains load and releases only the Rig provider route', async () => {
    const s = load({enabled: true}); await flush();
    const pending = deferred(), releases = [];
    const raw = {loadPlan: () => pending.promise, releaseRoute: async request => {
        releases.push(request); return {outcome: 'handled'};
    }};
    s.context.slopsmith.audioEffects = raw;
    const loading = s.guard.audio(raw, true).loadPlan({});
    const rejected = assert.rejects(loading, e => e.code === 'amp-sims-disabled');
    const off = s.set(false); await flush(); assert.equal(releases.length, 0);
    pending.resolve({outcome: 'handled'}); await rejected; await off;
    assert.equal(releases.length, 1);
    assert.equal(releases[0].providerId, 'rig_builder.effects');
    assert.equal(releases[0].routeKey, 'desktop-main');
});

test('host policy refusal is terminal and cannot enter the legacy load fallback', async () => {
    const s = load({enabled: true}); await flush();
    s.context.slopsmith.audioEffects = {loadPlan: async () => ({outcome: 'denied', code: 'amp-sims-disabled'})};
    await assert.rejects(s.context.rbLoadNativePresetPayload(s.context.rbAudioApi(),
        {id: 1, native_preset: {chain: []}}, {}), e => e.code === 'amp-sims-disabled');
    assert.deepEqual(s.effects, []);
});

test('late song response across off/on cannot load or mark a chain active', async () => {
    const s = load({enabled: true}); await flush();
    const body = deferred(), requested = deferred();
    s.context.fetch = async url => {
        if (String(url).includes('/mega_chain/')) {
            requested.resolve(); return {ok: true, json: () => body.promise};
        }
        return {ok: true, json: async () => ({})};
    };
    const build = s.mega.buildForSong('song.feedpak');
    await requested.promise; await s.set(false); await s.set(true);
    body.resolve({native_preset: {chain: []}, tones: []});
    assert.equal(await build, false);
    assert.equal(s.mega.isActive(), false);
    assert.equal(s.mega.isPending(), false);
    assert.deepEqual(s.effects, []);
});

test('older hosts and pure browser retain tone allowance; own Rig opt-out still wins', async () => {
    for (const options of [{service: false}, {applicable: false}]) {
        const s = load(options); await flush(); assert.equal(s.guard.allowed(), true);
        s.context.__rbEnabled = false; assert.equal(s.guard.allowed(), false);
    }
});

test('late active-tone continuation cannot reacquire a fresh gain/normalizer API after off/on', async () => {
    const s = load({enabled: true}); await flush();
    const activation = deferred(), entered = deferred(), calls = [];
    const tone = {tone_key: 'old', slots: [{idx: 0, bypassed: false}]};
    const context = vm.createContext({
        RbAmpPolicy: s.guard, _api: s.context.rbAudioApi,
        _mega: {tones: [tone], native_preset: {chain: [{type: 1}]}},
        _loadedViaAudioEffects: true, _indexToSlotId: [1], _activeToneKey: 'unchanged',
        _findToneByKey: () => tone, rbToneSegmentId: () => 'old',
        rbActivateSegmentWithHost: () => {entered.resolve(); return activation.promise;},
        _bypassFlipMuted: async () => calls.push('bypass'),
        rbApplyChainInputDrive: async () => calls.push('drive'),
        rbStartFinalChainNormalizer: async () => calls.push('normalizer'),
        _pokeLevelerRelock: async () => calls.push('relock'), _emitState: () => calls.push('state'),
    });
    const start = source.indexOf('    async function _applyActiveTone(');
    const end = source.indexOf('    async function buildForSong(', start);
    vm.runInContext(source.slice(start, end), context);
    const changing = context._applyActiveTone('old');
    await entered.promise; await s.set(false); await s.set(true);
    activation.resolve(false); await changing;
    assert.deepEqual(calls, []);
    assert.equal(context._activeToneKey, 'unchanged');
});
