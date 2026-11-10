#pragma once

#include "duckdb/common/common.hpp"

namespace duckdb {

//! A loopback fixture with no declared file size and chunked responses.
class SQLLogicHTTPServer {
public:
	explicit SQLLogicHTTPServer(const string &path);
	~SQLLogicHTTPServer();

	const string &GetURL() const;

private:
	struct Impl;
	unique_ptr<Impl> impl;
};

} // namespace duckdb
