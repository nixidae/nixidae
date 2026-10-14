use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct Greeting {
    text: String,
}

fn main() {
    let greeting: Greeting = serde_json::from_str(r#"{"text": "hello from serde-app"}"#).unwrap();
    println!("{}", serde_json::to_string(&greeting).unwrap());
}
