fn main() {
    println!("{} {}", greeting::greet(), itoa::Buffer::new().format(42));
}
