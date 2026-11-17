// Wrong output: off by one.
#include <iostream>

int main() {
    long long a = 0, b = 0;
    std::cin >> a >> b;
    std::cout << a + b + 1 << std::endl;
    return 0;
}
