// Runtime failure: reads its input, then throws an exception nobody catches, which lands as
// SIGABRT. Deliberately not an optimiser-dependent trick like dividing by a zero variable.
#include <iostream>
#include <stdexcept>
#include <vector>

int main() {
    long long a = 0, b = 0;
    std::cin >> a >> b;
    std::vector<long long> values;
    values.push_back(a);
    // .at() is bounds checked, so this throws std::out_of_range and terminates the process.
    std::cout << values.at(static_cast<size_t>(5)) + b << std::endl;
    return 0;
}
