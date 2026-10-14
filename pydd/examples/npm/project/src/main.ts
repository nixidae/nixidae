// chalk 4 finds the ansi-styles 4 that the lock nests under it.
const chalk = require("chalk");
const nested = require.resolve("ansi-styles", { paths: [require.resolve("chalk")] });

const plain = new chalk.Instance({ level: 0 });
console.log(plain.bold("hello from npm-demo"), nested.includes("chalk/node_modules") ? "nested" : "flat");
