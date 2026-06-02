import {memo, useState, useEffect} from 'react';
import { useTranslation } from 'react-i18next';
import { getMatchesCountText, paginationLabels, pageSizePreference, EmptyState } from '../config/GlobalConfigurations';

import { useCollection } from '@cloudscape-design/collection-hooks';
import {CollectionPreferences,Pagination } from '@cloudscape-design/components';
import TextFilter from "@cloudscape-design/components/text-filter";

import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";


const TableComponent = memo(({columnsTable,visibleContent, dataset, title, description = "", onSelectionItem = () => {}, pageSize = 10, extendedTableProperties = {}, tableActions = null, selectedListItems = [{ identifier: "" }], onPreferencesChange = () => {}  }) => {

    const { t } = useTranslation();

    const [selectedItems,setSelectedItems] = useState(selectedListItems);

    const visibleContentPreference = {
              title: 'Select visible content',
              options: [
                {
                  label: 'Main properties',
                  options: columnsTable.map(({ id, header }) => ({ id, label: header, editable: id !== 'id' })),
                },
              ],
    };

   const collectionPreferencesProps = {
            pageSizePreference,
            visibleContentPreference,
            cancelLabel: 'Cancel',
            confirmLabel: 'Confirm',
            title: 'Preferences',
    };


    const [preferences, setPreferences] = useState({ pageSize: pageSize, visibleContent: visibleContent });

    const { items, actions, filteredItemsCount, collectionProps, filterProps, paginationProps } = useCollection(
                dataset,
                {
                  filtering: {
                    empty: <EmptyState title={t('common.labels.no-records')} />,
                    noMatch: (
                      <EmptyState
                        title={t('common.labels.no-matches')}
                        action={<Button onClick={() => actions.setFiltering('')}>{t('common.actions.clear-filter')}</Button>}
                      />
                    ),
                  },
                  pagination: { pageSize: preferences.pageSize },
                  sorting: {},
                  selection: {},
                }
    );

    function onSelectionChange(item){
      onSelectionItem(item);
    }


    useEffect(() => {
        // Preserve selection during refresh by matching identifiers
        if (selectedItems.length > 0 && selectedItems[0].identifier && selectedItems[0].identifier !== "") {
            const preservedSelection = selectedItems.map(selectedItem => {
                // Find matching item in new dataset by identifier
                const matchingItem = dataset.find(dataItem => dataItem.identifier === selectedItem.identifier);
                return matchingItem || selectedItem; // Use matching item or keep original if not found
            }).filter(item => item); // Remove any null/undefined items

            // Only update if we found matching items
            if (preservedSelection.length > 0) {
                setSelectedItems(preservedSelection);
                return;
            }
        }

        // Fallback to prop value if no preservation needed
        setSelectedItems(selectedListItems);

    }, [selectedListItems, dataset]);

    return (
                <Table
                      {...collectionProps} // nosemgrep: react-props-spreading
                      selectionType="single"
                      header={
                        <Header
                          variant="h2"
                          counter= {"(" + dataset.length + ")"}
                          description={description}
                          actions={tableActions}
                        >
                          {title}
                        </Header>
                      }
                      columnDefinitions={columnsTable}
                      visibleColumns={preferences.visibleContent}
                      items={items}
                      pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />} // nosemgrep: react-props-spreading
                      filter={
                        <TextFilter
                          {...filterProps} // nosemgrep: react-props-spreading
                          countText={getMatchesCountText(filteredItemsCount)}
                          filteringAriaLabel="Filter records"
                        />
                      }
                      preferences={
                        <CollectionPreferences
                          {...collectionPreferencesProps} // nosemgrep: react-props-spreading
                          preferences={preferences}
                          onConfirm={({ detail }) => {
                            setPreferences(detail);
                            onPreferencesChange(detail);
                          }}
                        />
                      }
                      onSelectionChange={({ detail }) => {
                          onSelectionChange(detail.selectedItems);
                          setSelectedItems(detail.selectedItems);
                          }
                        }
                      selectedItems={selectedItems}
                      resizableColumns
                      stickyHeader
                      loadingText="Loading records"
                      {...extendedTableProperties} // nosemgrep: react-props-spreading
                    />

           );
});

export default TableComponent;
