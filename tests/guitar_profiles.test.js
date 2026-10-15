const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'screen.js'), 'utf8');

function setup({profile = true, enabled = true} = {}) {
    const gains = [], saves = [], requests = [], events = new Map();
    const nodes = new Map(['rb-amp-drive-val', 'rb-input-guitar-name'].map(id => [id, {}]));
    const listeners = [];
    let snapshot = {activeId: 'default', activeName: 'Default guitar', gain: 2, routeKey: 'input-1', revision: 1};
    const service = {
        ready: Promise.resolve(), getSnapshot: () => snapshot,
        async captureContext() {return {profileId: snapshot.activeId, routeKey: snapshot.routeKey, revision: snapshot.revision};},
        async saveGain(gain, context) {
            assert.equal(context.profileId, snapshot.activeId);
            assert.equal(context.routeKey, snapshot.routeKey);
            assert.equal(context.revision, snapshot.revision);
            saves.push({gain, ...context});
            snapshot = {...snapshot, gain, revision: snapshot.revision + 1};
            listeners.forEach(fn => fn(snapshot));
            return snapshot;
        },
        subscribe(fn) {listeners.push(fn); return () => {};},
    };
    const policy = {version: 1, snapshot: () => ({applicable: true, ready: true, enabled, generation: 1}),
        subscribe(fn) {fn(this.snapshot()); return () => {};}};
    const context = vm.createContext({
        console: {log() {}, warn() {}, error() {}},
        document: {getElementById: id => nodes.get(id) || null, querySelectorAll: () => [],
            createElement: () => ({classList: {add() {}, remove() {}}, addEventListener() {}}),
            addEventListener() {}, head: {appendChild() {}}, body: {appendChild() {}, contains: () => true}},
        localStorage: {getItem: () => null, setItem() {}},
        addEventListener(name, fn) {events.set(name, fn);}, dispatchEvent() {}, CustomEvent: class {},
        setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
        requestAnimationFrame: () => 0,
        fetch: async (url, options) => {requests.push({url, options}); return {ok: true, json: async () => ({})};},
        slopsmith: {on() {}}, feedBack: {ampSims: policy, ...(profile ? {guitarProfiles: service} : {})},
        feedBackDesktop: {audio: {async setGain(kind, value) {gains.push([kind, value]);}}},
    });
    context.window = context;
    vm.runInContext(`(() => {${source}\nrbSetRouteGainsWithHost = async () => false;
        Object.assign(window, {rbInputCalibration, rbApplyChainInputDrive, rbSetDesktopInput});})();`, context);
    context.__rbChainInputDrive = 3;
    context.__rbInputCalibration = 2;
    return {context, service, gains, saves, requests, nodes, events,
        change(next) {snapshot = {...snapshot, ...next, revision: snapshot.revision + 1}; listeners.forEach(fn => fn(snapshot));}};
}

test('profile calibration is read for UI but never multiplied into tone drive again', async () => {
    const s = setup();
    assert.equal(s.context.rbInputCalibration(), 2);
    await s.context.rbApplyChainInputDrive({isBass: false});
    s.change({activeId: 'strat', activeName: 'Strat', gain: 4});
    await s.context.rbApplyChainInputDrive({isBass: false});
    assert.deepEqual(s.gains, [['input', 3], ['input', 3]]);
    assert.equal(s.context.rbInputCalibration(), 4);
    assert.equal(s.nodes.get('rb-input-guitar-name').textContent, 'Strat');
    assert.equal(s.saves.length, 0);
});

test('legacy hosts still combine and persist calibration with tone drive', async () => {
    const s = setup({profile: false});
    await s.context.rbApplyChainInputDrive({isBass: false});
    assert.deepEqual(s.gains, [['input', 6]]);
    await s.context.rbSetDesktopInput(0);
    const saved = s.requests.find(r => r.options?.method === 'POST');
    assert.deepEqual(JSON.parse(saved.options.body), {nam_input_calibration: 1});
});

test('profile edits save while internal amps are off without tone gain writes', async () => {
    const s = setup({enabled: false});
    await s.context.rbSetDesktopInput(6);
    assert.equal(s.saves.length, 1);
    assert.ok(Math.abs(s.saves[0].gain - Math.pow(10, 6 / 20)) < 1e-10);
    assert.equal(s.saves[0].profileId, 'default');
    assert.deepEqual(s.gains, []);
    assert.equal(s.requests.filter(r => r.options?.method === 'POST').length, 0);
});

test('continuous edits are serialized with current revisions', async () => {
    const s = setup();
    await Promise.all([s.context.rbSetDesktopInput(1), s.context.rbSetDesktopInput(2), s.context.rbSetDesktopInput(3)]);
    assert.deepEqual(s.saves.map(s => s.revision), [1, 2, 3]);
    assert.ok(Math.abs(s.context.rbInputCalibration() - Math.pow(10, 3 / 20)) < 1e-10);
});

test('profile Input controls cover and respect the shared calibration range', async () => {
    const s = setup();
    await s.context.rbSetDesktopInput(-24);
    assert.equal(s.saves.at(-1).gain, 0.1);
    assert.equal(s.nodes.get('rb-amp-drive-val').textContent, '-20.0 dB');
    await s.context.rbSetDesktopInput(14);
    assert.equal(s.saves.at(-1).gain, 5);
    assert.equal(s.nodes.get('rb-amp-drive-val').textContent, '+14.0 dB');
});

test('manual edits can recover after an earlier startup failure', async () => {
    const s = setup();
    s.service.ready = Promise.reject(new Error('Earlier server failure'));
    s.service.ready.catch(() => {});
    await s.context.rbSetDesktopInput(3);
    assert.equal(s.saves.length, 1);
});

test('a queued edit cannot move to another guitar or input', async () => {
    for (const next of [{activeId: 'other'}, {routeKey: 'input-2'}]) {
        const s = setup();
        const save = s.context.rbSetDesktopInput(6);
        s.change(next);
        await save;
        assert.deepEqual(s.saves, []);
        assert.equal(s.nodes.get('rb-input-guitar-name').textContent, 'Input changed; adjust again');
    }
});

test('an external same-guitar revision change invalidates a queued edit', async () => {
    const s = setup();
    const save = s.context.rbSetDesktopInput(6);
    s.change({gain: 0.5});
    await save;
    assert.deepEqual(s.saves, []);
    assert.equal(s.context.rbInputCalibration(), 0.5);
});

test('legacy calibration event acknowledges without overwriting a profile', () => {
    const s = setup(); let acknowledged = false;
    s.events.get('rig-builder:set-input-calibration')({detail: {gain: 4, ack() {acknowledged = true;}}});
    assert.equal(acknowledged, true);
    assert.equal(s.context.rbInputCalibration(), 2);
    assert.deepEqual(s.saves, []);
});
