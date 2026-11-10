#include "catch.hpp"
#include "test_helpers.hpp"
#include "../sqlite/sqllogic_test_runner.hpp"
#include "duckdb/common/local_file_system.hpp"
#include "duckdb/common/path.hpp"
#include "duckdb/common/types/uuid.hpp"

using namespace duckdb;

TEST_CASE("SQLLogicTestRunner cleans only owned database paths", "[api]") {
	LocalFileSystem fs;
	auto root = Path::FromString(TestMakeAbsolute(TestDirectoryPath(), TestGetCurrentDirectory()));
	auto root_path = root.ToString();
	auto suffix = UUID::ToString(UUID::GenerateRandomUUID());
	auto owned_db = TestJoinPath(root_path, "sqllogic_owned_" + suffix + ".db");
	auto sibling_dir = root_path + "-sibling-" + suffix;
	auto escape_name = "sqllogic_escape_" + suffix;
	auto escape_dir = root.Parent().Join(escape_name).ToString();
	fs.CreateDirectory(sibling_dir);
	fs.CreateDirectory(escape_dir);
	auto sibling_db = TestJoinPath(sibling_dir, "control.db");
	auto escaped_db =
	    root_path + root.GetSeparator() + ".." + root.GetSeparator() + escape_name + root.GetSeparator() + "control.db";
	{
		SQLLogicTestRunner runner("");
		runner.LoadDatabase("", false);
		REQUIRE(runner.loaded_databases.empty());
		runner.LoadDatabase(IN_MEMORY_PATH, false);
		REQUIRE(runner.loaded_databases.empty());
		for (const auto &path : {owned_db, sibling_db, escaped_db}) {
			runner.LoadDatabase(path, false);
			auto result = runner.con->Query("CREATE TABLE ownership_marker(i INTEGER)");
			REQUIRE(!result->HasError());
			REQUIRE(fs.FileExists(path));
		}
		runner.test_succeeded = true;
	}
	REQUIRE(fs.FileExists(owned_db) == !DatabaseDestroyFires(true));
	REQUIRE(fs.FileExists(sibling_db));
	REQUIRE(fs.FileExists(escaped_db));
	fs.RemoveDirectory(sibling_dir);
	fs.RemoveDirectory(escape_dir);
}
