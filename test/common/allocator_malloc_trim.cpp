#include "duckdb/common/allocator.hpp"

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <mutex>
#include <thread>
#include <unistd.h>

static std::atomic<int64_t> tick_ms {0};
static std::atomic<uint64_t> trim_calls {0};
static std::atomic<uint64_t> last_pad {0};
static std::mutex trim_mutex;
static std::condition_variable trim_cv;
static bool trim_entered = false;
static bool trim_released = false;
static uint64_t failures = 0;

extern "C" int __real_malloc_trim(size_t pad);

extern "C" int64_t __wrap__ZN6duckdb9TimePoint9GetTickMsEv() {
	return tick_ms.load();
}

extern "C" int __wrap_malloc_trim(size_t pad) {
	last_pad = pad;
	if (++trim_calls == 1) {
		std::unique_lock<std::mutex> lock(trim_mutex);
		trim_entered = true;
		trim_cv.notify_all();
		trim_cv.wait(lock, [] { return trim_released; });
	}
	return __real_malloc_trim(pad);
}

static void Check(bool condition, const char *message) {
	if (!condition) {
		std::fprintf(stderr, "Allocator malloc_trim: %s\n", message);
		failures++;
	}
}

int main() {
	// A blocking lock in the flush path must fail instead of hanging the test runner.
	alarm(10);
	std::thread first([] { duckdb::Allocator::ThreadFlush(true, 1, 1); });
	{
		std::unique_lock<std::mutex> lock(trim_mutex);
		trim_cv.wait(lock, [] { return trim_entered; });
	}
	Check(trim_calls == 1, "the first flush must trim");
	Check(last_pad == 1, "the flush must preserve its pad");

	// A slow trim outlives the existing 100 ms start-based interval.
	tick_ms = 100;
	duckdb::Allocator::FlushAll();
	Check(trim_calls == 1, "a concurrent flush must skip an in-progress trim");

	tick_ms = 101;
	{
		std::lock_guard<std::mutex> lock(trim_mutex);
		trim_released = true;
	}
	trim_cv.notify_all();
	first.join();
	const auto completed_calls = trim_calls.load();

	tick_ms = 200;
	duckdb::Allocator::ThreadFlush(false, 0, 1);
	Check(trim_calls == completed_calls, "the cooldown must start when the previous trim finishes");

	tick_ms = 201;
	duckdb::Allocator::FlushAll();
	Check(trim_calls == completed_calls + 1, "trimming must resume at the end of the cooldown");
	Check(last_pad == 0, "FlushAll must pass zero pad to malloc_trim");

	tick_ms = 0;
	const auto calls_before_clock_step = trim_calls.load();
	duckdb::Allocator::ThreadFlush(false, 0, 1);
	Check(trim_calls == calls_before_clock_step, "a backwards clock step must not bypass the cooldown");

	std::printf("Allocator malloc_trim: %llu failure(s)\n", static_cast<unsigned long long>(failures));
	return failures ? 1 : 0;
}
