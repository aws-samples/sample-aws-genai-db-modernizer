from src.contracts.schema_design_input import (
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentTable,
    ForeignKeyAction,
)
from src.contracts.schema_design_input import NormalizedDataType as N
from src.tools.schema.aurora_common.ddl_generator import generate_mysql_ddl


def _users():
    return AgentTable(
        table_id="t1",
        table_name="users",
        row_count=10,
        primary_key=["id"],
        columns=[
            AgentColumn(
                column_name="id",
                normalized_data_type=N.integer,
                nullable=False,
                is_auto_increment=True,
            ),
            AgentColumn(
                column_name="email", normalized_data_type=N.string, max_length=255, nullable=False
            ),
        ],
        indexes=[AgentIndex(index_name="ix_users_email", columns=["email"], is_unique=True)],
    )


def test_mysql_uses_backticks_and_auto_increment():
    ddl = generate_mysql_ddl([_users()]).full_ddl
    assert "CREATE TABLE `users`" in ddl
    assert "`id` BIGINT AUTO_INCREMENT NOT NULL" in ddl
    assert "`email` VARCHAR(255) NOT NULL" in ddl
    assert "PRIMARY KEY (`id`)" in ddl
    assert "CREATE UNIQUE INDEX `ix_users_email` ON `users` (`email`)" in ddl


def test_mysql_foreign_key_backticked_after_tables():
    orders = AgentTable(
        table_id="t2",
        table_name="orders",
        row_count=1,
        primary_key=["id"],
        columns=[
            AgentColumn(
                column_name="id",
                normalized_data_type=N.integer,
                nullable=False,
                is_auto_increment=True,
            ),
            AgentColumn(column_name="user_id", normalized_data_type=N.integer, nullable=False),
        ],
        foreign_keys=[
            AgentForeignKey(
                constraint_name="fk_orders_user",
                columns=["user_id"],
                referenced_table="users",
                referenced_columns=["id"],
                on_delete=ForeignKeyAction.CASCADE,
            )
        ],
    )
    ddl = generate_mysql_ddl([_users(), orders]).full_ddl
    assert (
        "ALTER TABLE `orders` ADD CONSTRAINT `fk_orders_user` "
        "FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE"
    ) in ddl
    assert ddl.index("ALTER TABLE") > ddl.index("CREATE TABLE `orders`")


def test_mysql_decimal_is_residual():
    tbl = AgentTable(
        table_id="t3",
        table_name="prices",
        row_count=1,
        primary_key=["id"],
        columns=[
            AgentColumn(column_name="id", normalized_data_type=N.integer, nullable=False),
            AgentColumn(column_name="amount", normalized_data_type=N.decimal, nullable=True),
        ],
    )
    result = generate_mysql_ddl([tbl])
    assert any(r["column"] == "amount" for r in result.residuals)
    assert "`amount` DECIMAL(38,10)" in result.full_ddl


def test_mysql_composite_primary_key():
    tbl = AgentTable(
        table_id="t",
        table_name="memberships",
        row_count=1,
        primary_key=["org_id", "user_id"],
        columns=[
            AgentColumn(column_name="org_id", normalized_data_type=N.integer, nullable=False),
            AgentColumn(column_name="user_id", normalized_data_type=N.integer, nullable=False),
        ],
    )
    ddl = generate_mysql_ddl([tbl]).full_ddl
    assert "PRIMARY KEY (`org_id`, `user_id`)" in ddl


def test_mysql_multi_column_index_and_fk():
    parent = AgentTable(
        table_id="p",
        table_name="orgs",
        row_count=1,
        primary_key=["id"],
        columns=[AgentColumn(column_name="id", normalized_data_type=N.integer, nullable=False)],
    )
    child = AgentTable(
        table_id="c",
        table_name="members",
        row_count=1,
        primary_key=["org_id", "user_id"],
        columns=[
            AgentColumn(column_name="org_id", normalized_data_type=N.integer, nullable=False),
            AgentColumn(column_name="user_id", normalized_data_type=N.integer, nullable=False),
        ],
        indexes=[
            AgentIndex(index_name="ix_members_pair", columns=["org_id", "user_id"], is_unique=False)
        ],
        foreign_keys=[
            AgentForeignKey(
                constraint_name="fk_members_org",
                columns=["org_id"],
                referenced_table="orgs",
                referenced_columns=["id"],
                on_delete=None,
            )
        ],
    )
    ddl = generate_mysql_ddl([parent, child]).full_ddl
    assert "CREATE INDEX `ix_members_pair` ON `members` (`org_id`, `user_id`)" in ddl
    assert "FOREIGN KEY (`org_id`) REFERENCES `orgs` (`id`)" in ddl


def test_mysql_backtick_in_identifier_is_escaped():
    tbl = AgentTable(
        table_id="t",
        table_name="we`ird",
        row_count=1,
        primary_key=["c`x"],
        columns=[AgentColumn(column_name="c`x", normalized_data_type=N.integer, nullable=False)],
    )
    ddl = generate_mysql_ddl([tbl]).full_ddl
    # An embedded backtick is doubled.
    assert "CREATE TABLE `we``ird`" in ddl
    assert "`c``x`" in ddl


def test_mysql_auto_increment_suppresses_default():
    tbl = AgentTable(
        table_id="t",
        table_name="seqs",
        row_count=1,
        primary_key=["id"],
        columns=[
            AgentColumn(
                column_name="id",
                normalized_data_type=N.integer,
                nullable=False,
                is_auto_increment=True,
                default_value=0,
            ),
        ],
    )
    ddl = generate_mysql_ddl([tbl]).full_ddl
    assert "AUTO_INCREMENT" in ddl
    assert "DEFAULT 0" not in ddl
