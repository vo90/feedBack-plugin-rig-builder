const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'screen.js'), 'utf8');

function setup() {
    const requests = [], network = [];
    const host = {async loadPlan(request) {
        requests.push(request);
        const resolved = await context.rbFetchAudioEffectsPayloadForRequest(request);
        return {outcome: resolved.outcome, reason: resolved.reason, payload: {resolved}};
    }};
    const context = vm.createContext({Map, RB_API: '/rig', RB_PLUGIN_ID: 'rig_builder',
        RB_EFFECTS_ROUTE_KEY: 'input', RB_EFFECTS_PARTICIPANT_ID: 'rig_builder',
        rbState: {currentSongFile: 'current.feedpak'},
        rbRegisterAudioEffectsCapability() {}, rbAudioEffectsApi: () => host,
        rbPresetProviderRef: id => `preset:${id}`,
        rbPresetIdFromProviderRef: ref => ref?.startsWith('preset:') ? ref.slice(7) : '',
        async fetch(url) {network.push(url); return {ok: true, json: async () => ({remote: true})};},
    });
    const start = source.indexOf('const rbPendingEffectsPayloads =');
    const end = source.indexOf('async function rbReleaseAudioEffectsRouteWithHost(', start);
    vm.runInContext(source.slice(start, end), context);
    return {context, host, requests, network, size: () => vm.runInContext('rbPendingEffectsPayloads.size', context)};
}
const payload = id => ({...(id ? {id} : {}), native_preset: {chain: [{path: 'private/model.nam'}]}});

test('local song load reuses its response without a second HTTP request', async () => {
    const s = setup(), data = payload();
    const result = await s.context.rbLoadChainPlanWithHost(data, {mode: 'mega-chain', filename: 'song.feedpak'});
    assert.equal(result.audioEffects.payload.resolved.payload, data);
    assert.equal(result.audioEffects.payload.resolved.mode, 'mega-chain');
    assert.equal(s.network.length, 0);
    assert.equal(s.size(), 0);
    assert.ok(!JSON.stringify(s.requests[0]).includes('private/model.nam'));
});
test('preset loads retain full-chain resolution and preset identity', async () => {
    const s = setup(), data = payload(7);
    const result = await s.context.rbLoadChainPlanWithHost(data, {mode: 'preview'});
    assert.equal(result.audioEffects.payload.resolved.payload, data);
    assert.equal(result.audioEffects.payload.resolved.mode, 'full-chain');
    assert.equal(result.audioEffects.payload.resolved.ref, 7);
    assert.equal(s.network.length, 0);
});
test('external provider requests still fetch current song/preset data', async () => {
    const s = setup();
    await s.context.rbFetchAudioEffectsPayloadForRequest({target: {filename: 'other.feedpak'}});
    await s.context.rbFetchAudioEffectsPayloadForRequest({target: {providerRef: 'preset:4'}});
    assert.deepEqual(s.network, ['/rig/mega_chain/other.feedpak', '/rig/native_preset_full/4']);
});
test('host rejection or unsupported outcome releases pending data', async () => {
    for (const reject of [false, true]) {
        const s = setup();
        s.host.loadPlan = async () => {if(reject)throw Error('executor failed'); return {outcome: 'unavailable'};};
        await assert.rejects(s.context.rbLoadChainPlanWithHost(payload(1), {}));
        assert.equal(s.size(), 0);
    }
});
test('concurrent loads of the same target keep their own responses', async () => {
    const s = setup(), pending = [];
    s.host.loadPlan = request => new Promise(resolve => pending.push(async () => {
        resolve({outcome: 'handled', payload: {resolved: await s.context.rbFetchAudioEffectsPayloadForRequest(request)}});
    }));
    const a = payload(8), b = payload(8);
    a.version = 1; b.version = 2;
    const pa = s.context.rbLoadChainPlanWithHost(a, {}), pb = s.context.rbLoadChainPlanWithHost(b, {});
    await pending[1](); await pending[0]();
    assert.equal((await pa).audioEffects.payload.resolved.payload, a);
    assert.equal((await pb).audioEffects.payload.resolved.payload, b);
    assert.equal(s.size(), 0);
});
test('a token cannot resolve a different target or be replayed', async () => {
    const s = setup();
    s.host.loadPlan = async request => {
        const wrong = await s.context.rbFetchAudioEffectsPayloadForRequest({...request, target: {filename: 'wrong.feedpak'}});
        assert.equal(wrong.outcome, 'no-target');
        assert.equal((await s.context.rbFetchAudioEffectsPayloadForRequest(request)).outcome, 'handled');
        assert.equal((await s.context.rbFetchAudioEffectsPayloadForRequest(request)).outcome, 'no-target');
        return {outcome: 'handled'};
    };
    await s.context.rbLoadChainPlanWithHost(payload(), {mode: 'mega-chain', filename: 'song.feedpak'});
    assert.equal(s.size(), 0);
    assert.equal(s.network.length, 0);
});
test('late resolution after host timeout does not refetch or retain payload', async () => {
    const s = setup(); let request;
    s.host.loadPlan = async r => {request = r; return {outcome: 'failed', reason: 'timeout'};};
    await assert.rejects(s.context.rbLoadChainPlanWithHost(payload(2), {}));
    assert.equal((await s.context.rbFetchAudioEffectsPayloadForRequest(request)).outcome, 'no-target');
    assert.equal(s.size(), 0);
    assert.equal(s.network.length, 0);
});
test('missing local chain follows existing provider resolution', async () => {
    const s = setup();
    await s.context.rbLoadChainPlanWithHost({id: 4}, {});
    assert.deepEqual(s.network, ['/rig/native_preset_full/4']);
    assert.equal(s.size(), 0);
});
