#include "catch.hpp"
#include "test_helpers.hpp"
#include "duckdb/planner/expression/bound_conjunction_expression.hpp"
#include "duckdb/planner/expression/bound_function_expression.hpp"
#include "duckdb/planner/expression/bound_reference_expression.hpp"
#include "duckdb/planner/filter/expression_filter.hpp"
#include "duckdb/planner/filter/table_filter_functions.hpp"

using namespace duckdb;

namespace {

unique_ptr<Expression> Comparison() {
	return std::move(
	    ExpressionFilter::CreateComparisonFilter(ExpressionType::COMPARE_GREATERTHANOREQUALTO, Value::BIGINT(2700))
	        ->expr);
}

unique_ptr<Expression> AdaptiveComparison() {
	float threshold;
	idx_t vectors;
	GetThresholdAndVectorsToCheck(SelectivityOptionalFilterType::MIN_MAX, threshold, vectors);
	return CreateSelectivityOptionalFilterExpression(Comparison(), LogicalType::BIGINT, threshold, vectors);
}

bool CanCache(Connection &con, unique_ptr<Expression> expression) {
	ExpressionFilter filter(std::move(expression));
	auto state = TableFilterState::Initialize(*con.context, filter);
	return state->can_cache;
}

unique_ptr<Expression> InternalFilter(const ScalarFunction &function, unique_ptr<FunctionData> data) {
	vector<unique_ptr<Expression>> arguments;
	arguments.push_back(make_uniq<BoundReferenceExpression>(LogicalType::BIGINT, 0ULL));
	return make_uniq<BoundFunctionExpression>(BoundScalarFunction(function), std::move(arguments), std::move(data));
}

} // namespace

TEST_CASE("Table filter caching excludes adaptive, throwing and volatile evaluation", "[api][table_filter_cache]") {
	DuckDB db(nullptr);
	Connection con(db);
	REQUIRE(CanCache(con, Comparison()));
	REQUIRE_FALSE(CanCache(con, AdaptiveComparison()));
	for (auto type : {ExpressionType::CONJUNCTION_AND, ExpressionType::CONJUNCTION_OR}) {
		REQUIRE_FALSE(CanCache(con, make_uniq<BoundConjunctionExpression>(type, Comparison(), AdaptiveComparison())));
		REQUIRE_FALSE(CanCache(con, make_uniq<BoundConjunctionExpression>(type, AdaptiveComparison(), Comparison())));
	}
	auto throwing = Comparison();
	throwing->Cast<BoundFunctionExpression>().FunctionMutable().SetErrorMode(FunctionErrors::CAN_THROW_RUNTIME_ERROR);
	REQUIRE_FALSE(CanCache(con, std::move(throwing)));
	auto volatile_expression = Comparison();
	volatile_expression->Cast<BoundFunctionExpression>().FunctionMutable().SetStability(FunctionStability::VOLATILE);
	REQUIRE_FALSE(CanCache(con, std::move(volatile_expression)));

	// A statistics-only optional child is never evaluated.
	REQUIRE(CanCache(con, CreateOptionalFilterExpression(AdaptiveComparison(), LogicalType::BIGINT)));
	// Ordinary function arguments are evaluated, even for a statistics-only wrapper.
	vector<unique_ptr<Expression>> arguments;
	arguments.push_back(AdaptiveComparison());
	REQUIRE_FALSE(CanCache(con, make_uniq<BoundFunctionExpression>(
	                                BoundScalarFunction(OptionalFilterScalarFun::GetFunction(LogicalType::BOOLEAN)),
	                                std::move(arguments), make_uniq<OptionalFilterFunctionData>(nullptr))));
}

TEST_CASE("Bloom and prefix-range masks remain cacheable only without adaptive tracking", "[api][table_filter_cache]") {
	DuckDB db(nullptr);
	Connection con(db);
	for (bool adaptive : {false, true}) {
		float threshold;
		idx_t vectors;
		GetThresholdAndVectorsToCheck(SelectivityOptionalFilterType::BF, threshold, vectors);
		auto bloom_data = make_uniq<BloomFilterFunctionData>(shared_ptr<const BloomFilter>(), true, "k",
		                                                     LogicalType::BIGINT, threshold, adaptive ? vectors : 0);
		REQUIRE(CanCache(con, InternalFilter(BloomFilterScalarFun::GetFunction(LogicalType::BIGINT),
		                                     std::move(bloom_data))) == !adaptive);
		GetThresholdAndVectorsToCheck(SelectivityOptionalFilterType::PRF, threshold, vectors);
		auto prefix_data = make_uniq<PrefixRangeFunctionData>(nullptr, true, "k", LogicalType::BIGINT, threshold,
		                                                      adaptive ? vectors : 0);
		REQUIRE(CanCache(con, InternalFilter(PrefixRangeScalarFun::GetFunction(LogicalType::BIGINT),
		                                     std::move(prefix_data))) == !adaptive);
	}
	REQUIRE_FALSE(CanCache(
	    con, CreateDynamicFilterExpression(
	             make_shared_ptr<DynamicFilterData>(ExpressionType::COMPARE_GREATERTHANOREQUALTO, Value::BIGINT(2700)),
	             LogicalType::BIGINT)));
}
