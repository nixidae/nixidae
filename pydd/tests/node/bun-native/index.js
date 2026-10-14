#!/usr/bin/env node
const Database = require("better-sqlite3");

const db = new Database(":memory:");
const row = db.prepare("select sqlite_version() as version, 6 * 7 as answer").get();
console.log(`hello from native-npm sqlite=${row.version.split(".")[0]} answer=${row.answer}`);
