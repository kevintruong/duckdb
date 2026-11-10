#include "http_test_server.hpp"

#include "duckdb/common/exception.hpp"
#include "duckdb/common/string_util.hpp"
#include "httplib.hpp"

#include <fstream>
#include <iterator>
#include <thread>

namespace duckdb {

struct SQLLogicHTTPServer::Impl {
	duckdb_httplib::Server server;
	std::thread listener;
	string url;

	~Impl() {
		server.stop();
		if (listener.joinable()) {
			listener.join();
		}
	}

	void Start(const string &path) {
		std::ifstream input(path, std::ios::binary);
		if (!input.is_open()) {
			throw IOException("Could not open HTTP test fixture '%s'", path);
		}
		const string data((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
		if (input.bad()) {
			throw IOException("Could not read HTTP test fixture '%s'", path);
		}
		server.Get("/fixture", [data](const duckdb_httplib::Request &, duckdb_httplib::Response &response) {
			response.set_chunked_content_provider("text/csv", [data](idx_t offset, duckdb_httplib::DataSink &sink) {
				const idx_t length = data.size();
				if (offset >= length) {
					sink.done();
					return true;
				}
				const idx_t midpoint = length / 2;
				const idx_t count = offset < midpoint ? midpoint - offset : length - offset;
				if (!sink.write(data.data() + offset, count)) {
					return false;
				}
				if (offset + count == length) {
					sink.done();
				}
				return true;
			});
		});
		server.set_post_routing_handler([](const duckdb_httplib::Request &, duckdb_httplib::Response &response) {
			response.headers.erase("Content-Length");
			response.set_header("Accept-Ranges", "none");
			response.set_header("Connection", "close");
		});
		auto port = server.bind_to_any_port("127.0.0.1");
		if (port <= 0) {
			throw IOException("Could not bind the HTTP test fixture to loopback");
		}
		url = StringUtil::Format("http://127.0.0.1:%d/fixture", port);
		auto &http_server = server;
		listener = std::thread([&http_server]() { http_server.listen_after_bind(); });
		server.wait_until_ready();
		if (!server.is_running()) {
			throw IOException("Could not start the HTTP test fixture");
		}
	}
};

SQLLogicHTTPServer::SQLLogicHTTPServer(const string &path) : impl(make_uniq<Impl>()) {
	impl->Start(path);
}

SQLLogicHTTPServer::~SQLLogicHTTPServer() = default;

const string &SQLLogicHTTPServer::GetURL() const {
	return impl->url;
}

} // namespace duckdb
