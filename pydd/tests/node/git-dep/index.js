#!/usr/bin/env node
const isArray = require("isarray");
const ms = require("ms");

console.log(`hello from git-dep array=${isArray([])} ms=${ms(1000)}`);
