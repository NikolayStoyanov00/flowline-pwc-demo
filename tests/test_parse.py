"""Tests for the XML parser.

Two groups:

* Tests against small inline XML snippets, which pin down specific parsing
  rules and are readable on their own.
* Tests against the real sample export, which guard the headline numbers so a
  refactor cannot silently drop half the file.
"""

from __future__ import annotations

from collections import Counter

import pytest

from pwc2dbt.model import Kind, Repository
from pwc2dbt.parse import ParseError, parse_string

# ---------------------------------------------------------------------------
# Kind classification
# ---------------------------------------------------------------------------


class TestKindClassification:
    """A Union hides behind TYPE="Custom Transformation"; everything else is
    classified straight from TYPE."""

    @pytest.mark.parametrize(
        ("type_attr", "expected"),
        [
            ("Source Qualifier", Kind.SOURCE_QUALIFIER),
            ("Expression", Kind.EXPRESSION),
            ("Filter", Kind.FILTER),
            ("Joiner", Kind.JOINER),
            ("Sorter", Kind.SORTER),
            ("Aggregator", Kind.AGGREGATOR),
            ("Router", Kind.ROUTER),
            ("Source Definition", Kind.SOURCE_DEFINITION),
            ("Target Definition", Kind.TARGET_DEFINITION),
        ],
    )
    def test_plain_types_map_directly(self, type_attr: str, expected: Kind) -> None:
        assert Kind.classify(type_attr) is expected

    def test_union_is_recognised_by_template_name(self) -> None:
        assert (
            Kind.classify("Custom Transformation", "Union Transformation") is Kind.UNION
        )

    def test_other_custom_transformations_are_unknown(self) -> None:
        # A custom transformation we do not recognise must not be mistaken for
        # a Union, because that would silently emit wrong SQL.
        assert Kind.classify("Custom Transformation", "Java Transformation") is Kind.UNKNOWN
        assert Kind.classify("Custom Transformation", None) is Kind.UNKNOWN

    def test_unseen_type_is_unknown_not_an_error(self) -> None:
        # Parsing must not fail on an unsupported transformation; the converter
        # reports it later instead.
        assert Kind.classify("Lookup Procedure") is Kind.UNKNOWN


# ---------------------------------------------------------------------------
# Parsing rules, against small snippets
# ---------------------------------------------------------------------------


def _wrap(body: str) -> str:
    """Wrap XML fragments in the POWERMART/REPOSITORY/FOLDER envelope."""
    return (
        '<POWERMART CREATION_DATE="01/01/2026 00:00:00" REPOSITORY_VERSION="187.96">'
        '<REPOSITORY NAME="REPO_Test" VERSION="187" CODEPAGE="MS1252" DATABASETYPE="Oracle">'
        '<FOLDER NAME="FOLDER_Test">'
        f"{body}"
        "</FOLDER></REPOSITORY></POWERMART>"
    )


class TestEnvelope:
    def test_reads_repository_and_folder_names(self) -> None:
        repo = parse_string(_wrap(""))
        assert repo.name == "REPO_Test"
        assert repo.folder_name == "FOLDER_Test"
        assert repo.database_type == "Oracle"

    def test_rejects_a_non_powermart_root(self) -> None:
        with pytest.raises(ParseError, match="POWERMART"):
            parse_string("<SOMETHINGELSE/>")

    def test_rejects_malformed_xml(self) -> None:
        with pytest.raises(ParseError, match="malformed XML"):
            parse_string("<POWERMART><unclosed>")

    def test_requires_a_folder(self) -> None:
        with pytest.raises(ParseError, match="FOLDER"):
            parse_string(
                '<POWERMART><REPOSITORY NAME="R"></REPOSITORY></POWERMART>'
            )


class TestSourceAndTargetParsing:
    def test_source_keys_and_schema_are_captured(self) -> None:
        repo = parse_string(
            _wrap(
                '<SOURCE DATABASETYPE="Oracle" DBDNAME="XEPDB1_RAW" NAME="RAW_ORDERS"'
                ' OWNERNAME="RAW">'
                '<SOURCEFIELD DATATYPE="varchar2" FIELDNUMBER="1" KEYTYPE="PRIMARY KEY"'
                ' NAME="ID" NULLABLE="NOTNULL" PRECISION="100" SCALE="0"/>'
                '<SOURCEFIELD DATATYPE="varchar2" FIELDNUMBER="2" KEYTYPE="FOREIGN KEY"'
                ' NAME="CUSTOMER" NULLABLE="NOTNULL" PRECISION="100" SCALE="0"'
                ' REFERENCEDFIELD="ID" REFERENCEDTABLE="RAW_CUSTOMERS"/>'
                "</SOURCE>"
            )
        )
        table = repo.sources["RAW_ORDERS"]
        assert table.schema == "RAW"
        assert table.connection_name == "XEPDB1_RAW"

        pk, fk = table.primary_keys, table.foreign_keys
        assert [c.name for c in pk] == ["ID"]
        assert [c.name for c in fk] == ["CUSTOMER"]
        assert fk[0].referenced_table == "RAW_CUSTOMERS"
        assert fk[0].referenced_field == "ID"
        # NULLABLE="NOTNULL" means the column is *not* nullable.
        assert pk[0].nullable is False

    def test_columns_are_ordered_by_field_number_not_document_order(self) -> None:
        repo = parse_string(
            _wrap(
                '<TARGET NAME="T">'
                '<TARGETFIELD FIELDNUMBER="3" NAME="THIRD" DATATYPE="number"/>'
                '<TARGETFIELD FIELDNUMBER="1" NAME="FIRST" DATATYPE="number"/>'
                '<TARGETFIELD FIELDNUMBER="2" NAME="SECOND" DATATYPE="number"/>'
                "</TARGET>"
            )
        )
        assert [c.name for c in repo.targets["T"].columns] == [
            "FIRST",
            "SECOND",
            "THIRD",
        ]

    def test_interleaved_sources_targets_and_mappings_are_all_collected(self) -> None:
        # The real export does not group these; it alternates between them.
        repo = parse_string(
            _wrap(
                '<SOURCE NAME="S1"/>'
                '<TARGET NAME="T1"/>'
                '<MAPPING NAME="m_one"/>'
                '<SOURCE NAME="S2"/>'
                '<TARGET NAME="T2"/>'
                '<MAPPING NAME="m_two"/>'
            )
        )
        assert set(repo.sources) == {"S1", "S2"}
        assert set(repo.targets) == {"T1", "T2"}
        assert [m.name for m in repo.mappings] == ["m_one", "m_two"]


class TestPortParsing:
    def test_port_type_flags_are_decoded(self) -> None:
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="JNR" TYPE="Joiner">'
                '<TRANSFORMFIELD NAME="MASTER_KEY" PORTTYPE="INPUT/OUTPUT/MASTER"/>'
                '<TRANSFORMFIELD NAME="DETAIL_KEY" PORTTYPE="INPUT/OUTPUT"/>'
                '<TRANSFORMFIELD NAME="ONLY_IN" PORTTYPE="INPUT"/>'
                '<TRANSFORMFIELD NAME="v_SCRATCH" PORTTYPE="LOCAL VARIABLE"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        ports = {p.name: p for p in repo.mappings[0].transformations["JNR"].ports}

        master = ports["MASTER_KEY"]
        assert (master.is_input, master.is_output, master.is_master) == (True, True, True)

        detail = ports["DETAIL_KEY"]
        assert (detail.is_input, detail.is_output, detail.is_master) == (True, True, False)

        assert ports["ONLY_IN"].is_output is False
        assert ports["v_SCRATCH"].is_variable is True

    def test_embedded_newlines_in_expressions_are_collapsed(self) -> None:
        # The real file writes multi-line expressions with &#xD;&#xA; escapes.
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="EXP" TYPE="Expression">'
                '<TRANSFORMFIELD NAME="BAND" PORTTYPE="OUTPUT" EXPRESSIONTYPE="GENERAL"'
                " EXPRESSION=\"IIF(TAX_RATE &gt;= 0.15,&#xD;&#xA;'EMEA',&#xD;&#xA;'AMER')\"/>"
                "</TRANSFORMATION></MAPPING>"
            )
        )
        port = repo.mappings[0].transformations["EXP"].ports[0]
        assert port.expression == "IIF(TAX_RATE >= 0.15, 'EMEA', 'AMER')"

    def test_whitespace_inside_a_string_literal_is_kept(self) -> None:
        # The literal is data: collapsing it would compare against 'A B'
        # instead of 'A  B', and match different rows.
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="EXP" TYPE="Expression">'
                '<TRANSFORMFIELD NAME="F" PORTTYPE="OUTPUT" EXPRESSIONTYPE="GENERAL"'
                " EXPRESSION=\"IIF(NAME =&#xD;&#xA;  'A  B', 1, 0)\"/>"
                "</TRANSFORMATION></MAPPING>"
            )
        )
        port = repo.mappings[0].transformations["EXP"].ports[0]
        assert port.expression == "IIF(NAME = 'A  B', 1, 0)"

    def test_passthrough_expressions_are_identified(self) -> None:
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="EXP" TYPE="Expression">'
                '<TRANSFORMFIELD NAME="ORDER_ID" PORTTYPE="INPUT/OUTPUT"'
                ' EXPRESSION="ORDER_ID"/>'
                '<TRANSFORMFIELD NAME="TOTAL" PORTTYPE="OUTPUT" EXPRESSION="A + B"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        ports = {p.name: p for p in repo.mappings[0].transformations["EXP"].ports}
        assert ports["ORDER_ID"].is_passthrough is True
        assert ports["TOTAL"].is_passthrough is False

    def test_group_by_ports_are_flagged(self) -> None:
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="AGG" TYPE="Aggregator">'
                '<TRANSFORMFIELD NAME="LOCATION_ID" PORTTYPE="INPUT/OUTPUT"'
                ' EXPRESSION="LOCATION_ID" EXPRESSIONTYPE="GROUPBY"/>'
                '<TRANSFORMFIELD NAME="N" PORTTYPE="OUTPUT"'
                ' EXPRESSION="COUNT(ORDER_ID)" EXPRESSIONTYPE="GENERAL"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        ports = {p.name: p for p in repo.mappings[0].transformations["AGG"].ports}
        assert ports["LOCATION_ID"].is_group_by is True
        assert ports["N"].is_group_by is False


class TestTableAttributes:
    def test_empty_attribute_values_are_dropped(self) -> None:
        # Sql Query="" means "no SQL override", so it should not look like one.
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="SQ" TYPE="Source Qualifier">'
                '<TABLEATTRIBUTE NAME="Sql Query" VALUE=""/>'
                '<TABLEATTRIBUTE NAME="Select Distinct" VALUE="NO"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        attrs = repo.mappings[0].transformations["SQ"].attributes
        assert "Sql Query" not in attrs
        assert attrs["Select Distinct"] == "NO"


class TestInstanceIndirection:
    def test_instance_name_differs_from_definition_name(self) -> None:
        # Two reads of one table in one mapping: two nodes, one definition.
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m">'
                '<INSTANCE NAME="STG_ORDERS" TRANSFORMATION_NAME="STG_ORDERS"'
                ' TRANSFORMATION_TYPE="Source Definition" TYPE="SOURCE"/>'
                '<INSTANCE NAME="STG_ORDERS1" TRANSFORMATION_NAME="STG_ORDERS"'
                ' TRANSFORMATION_TYPE="Source Definition" TYPE="SOURCE"/>'
                "</MAPPING>"
            )
        )
        instances = repo.mappings[0].instances
        assert set(instances) == {"STG_ORDERS", "STG_ORDERS1"}
        # Both point at the same definition.
        assert instances["STG_ORDERS1"].definition_name == "STG_ORDERS"
        assert instances["STG_ORDERS1"].is_source is True

    def test_associated_source_instance_is_captured(self) -> None:
        # This is the only link from a Source Qualifier to a physical table.
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m">'
                '<INSTANCE NAME="SQ_RAW_CUSTOMERS" TRANSFORMATION_NAME="SQ_RAW_CUSTOMERS"'
                ' TRANSFORMATION_TYPE="Source Qualifier" TYPE="TRANSFORMATION">'
                '<ASSOCIATED_SOURCE_INSTANCE NAME="RAW_CUSTOMERS"/>'
                "</INSTANCE></MAPPING>"
            )
        )
        instance = repo.mappings[0].instances["SQ_RAW_CUSTOMERS"]
        assert instance.associated_sources == ("RAW_CUSTOMERS",)

    def test_target_pre_sql_is_captured(self) -> None:
        # The target schema is only recoverable from this TRUNCATE statement.
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m">'
                '<INSTANCE NAME="CUSTOMERS" TRANSFORMATION_NAME="CUSTOMERS"'
                ' TRANSFORMATION_TYPE="Target Definition" TYPE="TARGET">'
                '<TABLEATTRIBUTE NAME="Pre SQL" VALUE="TRUNCATE TABLE MART.CUSTOMERS"/>'
                "</INSTANCE></MAPPING>"
            )
        )
        instance = repo.mappings[0].instances["CUSTOMERS"]
        assert instance.is_target is True
        assert instance.attributes["Pre SQL"] == "TRUNCATE TABLE MART.CUSTOMERS"


class TestNameScoping:
    def test_same_transformation_name_in_two_mappings_stays_separate(self) -> None:
        # A global lookup table would silently return the wrong definition.
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m_one"><TRANSFORMATION NAME="SQ_STG_ORDERS"'
                ' TYPE="Source Qualifier">'
                '<TRANSFORMFIELD NAME="ONLY_IN_ONE" PORTTYPE="INPUT/OUTPUT"/>'
                "</TRANSFORMATION></MAPPING>"
                '<MAPPING NAME="m_two"><TRANSFORMATION NAME="SQ_STG_ORDERS"'
                ' TYPE="Source Qualifier">'
                '<TRANSFORMFIELD NAME="ONLY_IN_TWO" PORTTYPE="INPUT/OUTPUT"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        one = repo.mapping("m_one").transformations["SQ_STG_ORDERS"]
        two = repo.mapping("m_two").transformations["SQ_STG_ORDERS"]
        assert [p.name for p in one.ports] == ["ONLY_IN_ONE"]
        assert [p.name for p in two.ports] == ["ONLY_IN_TWO"]


class TestRouterAndUnionShapes:
    def test_router_groups_and_ref_fields(self) -> None:
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="RTR" TYPE="Router">'
                '<GROUP NAME="INPUT" ORDER="1" TYPE="INPUT"/>'
                '<GROUP NAME="G_APAC" ORDER="2" TYPE="OUTPUT"'
                " EXPRESSION=\"REGION = 'APAC'\"/>"
                '<GROUP NAME="DEFAULT1" ORDER="5" TYPE="OUTPUT/DEFAULT"/>'
                '<TRANSFORMFIELD NAME="LOC" PORTTYPE="INPUT" GROUP="INPUT"/>'
                '<TRANSFORMFIELD NAME="LOC1" PORTTYPE="OUTPUT" GROUP="G_APAC"'
                ' REF_FIELD="LOC"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        router = repo.mappings[0].transformations["RTR"]
        groups = {g.name: g for g in router.groups}

        assert groups["G_APAC"].expression == "REGION = 'APAC'"
        assert groups["G_APAC"].is_output is True
        assert groups["G_APAC"].is_default is False
        assert groups["DEFAULT1"].is_default is True
        assert groups["INPUT"].is_input is True
        # The default group carries no condition.
        assert groups["DEFAULT1"].expression is None

        # Output ports are per-group copies tied back by REF_FIELD.
        assert router.port("LOC1").ref_field == "LOC"
        assert router.port("LOC1").group == "G_APAC"

    def test_union_field_dependencies(self) -> None:
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="UN" TYPE="Custom Transformation"'
                ' TEMPLATENAME="Union Transformation">'
                '<GROUP NAME="OUTPUT" ORDER="1" TYPE="OUTPUT"/>'
                '<GROUP NAME="APAC" ORDER="2" TYPE="INPUT"/>'
                '<FIELDDEPENDENCY INPUTFIELD="LOC2" OUTPUTFIELD="LOC"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        union = repo.mappings[0].transformations["UN"]
        assert union.kind is Kind.UNION
        assert union.field_dependencies[0].input_field == "LOC2"
        assert union.field_dependencies[0].output_field == "LOC"


class TestSorterKeys:
    def test_sort_keys_come_from_ports_not_attributes(self) -> None:
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m"><TRANSFORMATION NAME="SRT" TYPE="Sorter">'
                '<TRANSFORMFIELD NAME="PRODUCT_ID" PORTTYPE="INPUT/OUTPUT"'
                ' ISSORTKEY="YES" SORTDIRECTION="ASCENDING"/>'
                '<TRANSFORMFIELD NAME="COST" PORTTYPE="INPUT/OUTPUT"'
                ' ISSORTKEY="NO" SORTDIRECTION="ASCENDING"/>'
                "</TRANSFORMATION></MAPPING>"
            )
        )
        sorter = repo.mappings[0].transformations["SRT"]
        assert [p.name for p in sorter.sort_keys] == ["PRODUCT_ID"]
        assert sorter.sort_keys[0].sort_direction == "ASCENDING"


class TestMappingVariables:
    def test_variable_is_captured_with_its_default(self) -> None:
        repo = parse_string(
            _wrap(
                '<MAPPING NAME="m">'
                '<MAPPINGVARIABLE DATATYPE="date/time" DEFAULTVALUE="12/31/2025 00:00:00"'
                ' NAME="$$RUN_DATE" PRECISION="29" SCALE="9"/>'
                "</MAPPING>"
            )
        )
        variable = repo.mappings[0].variables[0]
        assert variable.name == "$$RUN_DATE"
        assert variable.default_value == "12/31/2025 00:00:00"


# ---------------------------------------------------------------------------
# The real sample export
#
# These numbers were established by reading the file and are asserted here so
# that a parser change cannot quietly discard part of it.
# ---------------------------------------------------------------------------


class TestDemoExport:
    def test_envelope(self, repo: Repository) -> None:
        assert repo.name == "REPO_Jaffleshop"
        assert repo.folder_name == "FLOWLINE_DEMO_JAFFLESHOP"
        assert repo.database_type == "Oracle"

    def test_headline_counts(self, repo: Repository) -> None:
        assert len(repo.sources) == 12
        assert len(repo.targets) == 16
        assert len(repo.mappings) == 3
        assert sum(len(m.transformations) for m in repo.mappings) == 98
        assert sum(len(m.instances) for m in repo.mappings) == 142
        assert sum(len(m.connectors) for m in repo.mappings) == 1066

    def test_one_model_per_target_instance(self, repo: Repository) -> None:
        # The headline conversion fact: 3 mappings, but 16 dbt models.
        targets = [t for m in repo.mappings for t in m.target_instances]
        assert len(targets) == 16

    def test_transformation_kind_census(self, repo: Repository) -> None:
        kinds = Counter(
            t.kind for m in repo.mappings for t in m.transformations.values()
        )
        assert kinds == {
            Kind.SOURCE_QUALIFIER: 28,
            Kind.EXPRESSION: 25,
            Kind.JOINER: 16,
            Kind.SORTER: 15,
            Kind.AGGREGATOR: 9,
            Kind.ROUTER: 2,
            Kind.UNION: 2,
            Kind.FILTER: 1,
        }

    def test_no_unrecognised_transformations(self, repo: Repository) -> None:
        unknown = [
            f"{m.name}.{t.name} ({t.raw_type})"
            for m in repo.mappings
            for t in m.transformations.values()
            if t.kind is Kind.UNKNOWN
        ]
        assert unknown == []

    def test_mapping_names(self, repo: Repository) -> None:
        assert [m.name for m in repo.mappings] == [
            "m_FL_JS_STAGING_FULL",
            "m_FL_JS_MARTS_ENRICHED",
            "m_FL_JS_MARTS_CORE",
        ]

    def test_staging_mapping_targets_in_load_order(self, repo: Repository) -> None:
        staging = repo.mapping("m_FL_JS_STAGING_FULL")
        assert [t.name for t in staging.target_instances] == [
            "STG_CUSTOMERS",
            "STG_ORDERS",
            "STG_LOCATIONS",
            "STG_PRODUCTS",
            "STG_SUPPLIES",
            "STG_ORDER_ITEMS",
        ]

    def test_enriched_mapping_has_the_run_date_variable(self, repo: Repository) -> None:
        enriched = repo.mapping("m_FL_JS_MARTS_ENRICHED")
        assert [v.name for v in enriched.variables] == ["$$RUN_DATE"]
        # No other mapping declares variables.
        assert repo.mapping("m_FL_JS_MARTS_CORE").variables == ()
        assert repo.mapping("m_FL_JS_STAGING_FULL").variables == ()

    def test_duplicate_target_instance_name_is_preserved(self, repo: Repository) -> None:
        # ORDER_FINANCIALS is placed as an instance called ORDER_FINANCIALS1.
        enriched = repo.mapping("m_FL_JS_MARTS_ENRICHED")
        instance = enriched.instances["ORDER_FINANCIALS1"]
        assert instance.definition_name == "ORDER_FINANCIALS"
        assert instance.is_target is True

    def test_no_source_qualifier_has_a_sql_override(self, repo: Repository) -> None:
        # Established by inspection; if a future export has one we must notice,
        # because we do not translate hand-written SQL.
        overrides = [
            f"{m.name}.{t.name}"
            for m in repo.mappings
            for t in m.transformations.values()
            if t.kind is Kind.SOURCE_QUALIFIER
            and (t.attribute("Sql Query") or t.attribute("Source Filter"))
        ]
        assert overrides == []

    def test_every_source_qualifier_resolves_to_a_source_table(
        self, repo: Repository
    ) -> None:
        for mapping in repo.mappings:
            for instance in mapping.instances.values():
                if instance.kind is not Kind.SOURCE_QUALIFIER:
                    continue
                assert instance.associated_sources, (
                    f"{mapping.name}.{instance.name} has no "
                    "ASSOCIATED_SOURCE_INSTANCE"
                )
                for source_instance_name in instance.associated_sources:
                    source_instance = mapping.instances[source_instance_name]
                    assert source_instance.definition_name in repo.sources

    def test_every_target_instance_declares_its_schema_via_pre_sql(
        self, repo: Repository
    ) -> None:
        # The only place the target schema (STG / MART) appears.
        for mapping in repo.mappings:
            for target in mapping.target_instances:
                pre_sql = target.attributes.get("Pre SQL", "")
                assert pre_sql.upper().startswith("TRUNCATE TABLE "), (
                    f"{mapping.name}.{target.name} has no TRUNCATE Pre SQL: {pre_sql!r}"
                )

    def test_source_keys_are_available_for_dbt_tests(self, repo: Repository) -> None:
        # Target definitions carry no keys at all, so generated dbt tests have
        # to come from the RAW sources. Confirm both halves of that claim.
        raw_orders = repo.sources["RAW_ORDERS"]
        assert [c.name for c in raw_orders.primary_keys] == ["ID"]
        assert [c.name for c in raw_orders.foreign_keys] == ["CUSTOMER"]

        for table in repo.targets.values():
            assert table.primary_keys == ()
            assert table.foreign_keys == ()

    def test_all_sorters_are_ascending_and_non_distinct(self, repo: Repository) -> None:
        # Relied on later: it is what makes most sorters droppable.
        for mapping in repo.mappings:
            for transformation in mapping.transformations.values():
                if transformation.kind is not Kind.SORTER:
                    continue
                assert transformation.attribute("Distinct", "NO") == "NO"
                for port in transformation.sort_keys:
                    assert port.sort_direction == "ASCENDING"

    def test_connectors_into_stg_products_cross_the_price_columns(
        self, repo: Repository
    ) -> None:
        """Columns cannot be matched between steps by name.

        EXP_PRODUCTS computes PRODUCT_PRICE as ``PRICE/100`` (dollars) and
        PRODUCT_PRICE_CENTS as ``PRICE`` (cents), then the connectors swap
        them on the way into the target. Matching on name would silently
        produce different numbers, so this documents the behaviour the
        converter has to reproduce.
        """
        staging = repo.mapping("m_FL_JS_STAGING_FULL")

        ports = {p.name: p for p in staging.transformations["EXP_PRODUCTS"].ports}
        assert ports["PRODUCT_PRICE"].expression == "PRICE/100"
        assert ports["PRODUCT_PRICE_CENTS"].expression == "PRICE"

        wires = {
            c.from_field: c.to_field
            for c in staging.connectors
            if c.from_instance == "EXP_PRODUCTS" and c.to_instance == "STG_PRODUCTS"
        }
        assert wires["PRODUCT_PRICE"] == "PRODUCT_PRICE_CENTS"
        assert wires["PRODUCT_PRICE_CENTS"] == "PRODUCT_PRICE"

    def test_expressions_contain_no_line_breaks(self, repo: Repository) -> None:
        # Normalisation must have collapsed the embedded CR/LF pairs.
        for mapping in repo.mappings:
            for transformation in mapping.transformations.values():
                for port in transformation.ports:
                    if port.expression:
                        assert "\n" not in port.expression
                        assert "\r" not in port.expression
