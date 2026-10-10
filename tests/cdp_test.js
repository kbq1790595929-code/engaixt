var map = window.__hookText.map;
var fb = {};
for (var k in map) {
    var parts = k.split('|||');
    var textKey = parts[0];
    if (!fb[textKey]) fb[textKey] = map[k];
}
var testKey = Object.keys(map)[0];
var testParts = testKey.split('|||');
var textOnly = testParts[0];
var translated = fb[textOnly];
JSON.stringify({test: textOnly.substring(0,40), translated: translated ? translated.substring(0,40) : 'NOT FOUND', mapSize: Object.keys(map).length, fbSize: Object.keys(fb).length})
