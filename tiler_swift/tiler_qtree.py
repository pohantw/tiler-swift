import numpy

class Tiler_Qtree:

    def __init__( self, config, tensors, model ):
        self._config = config
        self._tensors = tensors
        self._model = model
    

    def _tile_recursive( self, rect ):
        x, y, width, height = rect

        # assumption: if the tile can have positive runtime estimate,
        # then its runtime must be better than its splits
        tile_runtime = self._model.estimate_tile_runtime( rect )
        if tile_runtime < 0:
            # the tile is infeasible, so we need to split it
            if width > height:
                hw = width // 2
                r1, t1 = self._tile_recursive( [x, y, hw, height] )
                r2, t2 = self._tile_recursive( [x + hw, y, width-hw, height] )
                return r1 + r2, t1 + t2
            else:
                hh = height // 2
                r1, t1 = self._tile_recursive( [x, y, width, hh] )
                r2, t2 = self._tile_recursive( [x, y + hh, width, height-hh] )
                return r1 + r2, t1 + t2
        else:
            result = {}
            for tensor_name in self._tensors.keys():
                result[tensor_name] = [x, y, width, height]
            return tile_runtime, [result]

    def _tile_recursive_simple( self, rect ):
        x, y, width, height = rect

        # loop through tensors, count the non-zeros in the tile
        nnzs = {}
        for tensor_name, tensor in self._tensors.items():
            tiled_tensor = tensor[y:y+height, x:x+width]
            nnzs[tensor_name] = numpy.count_nonzero(tiled_tensor)

        # check if the tile fits in the memory tile
        # TODO: also need to check output
        # TODO: now hard-coded to 50, need to change
        tile_fit_ok = True
        for tensor_name, nnz in nnzs.items():
            if nnz > 15:
                tile_fit_ok = False

        # if the tile fits, return the tile
        # otherwise, recursively split the tile
        # TODO: there might be overlaps in the tiles, need to fix
        if tile_fit_ok:
            result = {}
            for tensor_name in self._tensors.keys():
                result[tensor_name] = [x, y, width, height]
            return [result], True
        else:
            # +----+----+
            # | q1 | q2 |
            # +----+----+
            # | q3 | q4 |
            # +----+----+
            if width != 1:
                hw = width // 2
            else:
                hw = 1
            if height != 1:
                hh = height // 2
            else:
                hh = 1
            q1, q1_is_leaf = self._tile_recursive_simple( [x,      y,      hw,       hh] )
            q2, q2_is_leaf = self._tile_recursive_simple( [x + hw, y,      width-hw, hh] )
            q3, q3_is_leaf = self._tile_recursive_simple( [x,      y + hh, hw,       height-hh] )
            q4, q4_is_leaf = self._tile_recursive_simple( [x + hw, y + hh, width-hw, height-hh] )

            if self._config["qtree_tile_merging"]:
                q_result = [[q1, q2], [q3, q4]]
                q_is_leaf = [[q1_is_leaf, q2_is_leaf], [q3_is_leaf, q4_is_leaf]]
                
                merged_result = self._merge_tiles(q_result, q_is_leaf)

                return merged_result, False
            else:
                return q1 + q2 + q3 + q4, False

            


    def _merge_tiles( self, q_result, q_is_leaf ):
        # merge the quadrant if the quadrant is a leaf quadrant (no further qtree tiling)
        # and if the two leaf quadrant fit in the memory tile

        merged = [[False, False], [False, False]]
        quadrants_after_merge = []

        # try merging horizontally first, the merging tiles have to be leaf tiles
        if q_is_leaf[0][0] and q_is_leaf[0][1]:
            merge_result = self._try_merge_quadrants([q_result[0][0][0], q_result[0][1][0]], 'horizontal')
            if merge_result is not None:
                merged[0][0] = True
                merged[0][1] = True
                quadrants_after_merge.append(merge_result)
        
        if q_is_leaf[1][0] and q_is_leaf[1][1]:
            merge_result = self._try_merge_quadrants([q_result[1][0][0], q_result[1][1][0]], 'horizontal')
            if merge_result is not None:
                merged[1][0] = True
                merged[1][1] = True
                quadrants_after_merge.append(merge_result)

        # try merging veritcally, the merging tiles have to be leaf tiles and have not been merged horizontally
        if q_is_leaf[0][0] and q_is_leaf[1][0] and not merged[0][0] and not merged[1][0]:
            merge_result = self._try_merge_quadrants([q_result[0][0][0], q_result[1][0][0]], 'vertical')
            if merge_result is not None:
                merged[0][0] = True
                merged[1][0] = True
                quadrants_after_merge.append(merge_result)
        
        if q_is_leaf[0][1] and q_is_leaf[1][1] and not merged[0][1] and not merged[1][1]:
            merge_result = self._try_merge_quadrants([q_result[0][1][0], q_result[1][1][0]], 'vertical')
            if merge_result is not None:
                merged[0][1] = True
                merged[1][1] = True
                quadrants_after_merge.append(merge_result)
        
        # append the tiles that are not merged to the result list
        for i in range(2): 
            for j in range(2):
                if not merged[i][j]:
                    quadrants_after_merge += q_result[i][j]

        return quadrants_after_merge

    def _try_merge_quadrants(self, quadrant_info, merge_direction):
        # quadrant_info is a list of quadrants we want to merge
        # each quadrant is a dictionary indexed by the input tensor name
        # and stores the quadrant rectangle (x, y, width, height) for each input tensor
        # merge_direction specify the direction of merging, it's either 'horizontal' or 'vertical'
        assert (merge_direction == 'horizontal' or merge_direction == 'vertical'), "merge_direction must be either 'horizontal' or 'vertical'"

        # first count the nnzs in the quadrants we are trying to merge 
        nnz = {}
        for quadrant in quadrant_info:
            for tensor_name, tile_rect in quadrant.items():
                x, y, width, height = tile_rect
                tiled_tensor = self._tensors[tensor_name][y:y+height, x:x+width]
                if tensor_name not in nnz:
                    nnz[tensor_name] = 0
                nnz[tensor_name] += numpy.count_nonzero(tiled_tensor)

        # check if the combined quadrant fits in the memory tile
        # TODO: Po-Han please replace this with the performance model
        fit_ok = True
        for tensor_name, nnz in nnz.items():
            if nnz > 15:
                fit_ok = False
        
        # combine the quadrant if it fits
        if fit_ok:
            result = {}
            for quadrant in quadrant_info:
                for tensor_name, tile_rect in quadrant.items():
                    if not tensor_name in result:
                        result[tensor_name] = tile_rect
                    else:
                        if merge_direction == "horizontal":
                            # horizontally merging tile should have the same height, and have the sam y anchor
                            assert(tile_rect[1] == result[tensor_name][1] and tile_rect[3] == result[tensor_name][3])
                            # increment the width to refelct the merge
                            result[tensor_name][2] += tile_rect[2]
                        elif merge_direction == "vertical":
                            # vertically merging tile should have the same width, and have the same x anchor
                            assert(tile_rect[0] == result[tensor_name][0] and tile_rect[2] == result[tensor_name][2])
                            result[tensor_name][3] += tile_rect[3]
            return result
        else: 
            return None


    def tile( self ):
        assert len(self._tensors) == 2, "only support two input tensors"
        tensor_name = list(self._tensors.keys())[0]
        tensor_width = self._tensors[tensor_name].shape[1]
        tensor_height = self._tensors[tensor_name].shape[0]
        # result, _  = self._tile_recursive_simple([0, 0, tensor_width, tensor_height])
        run_time_estimate, result = self._tile_recursive([0, 0, tensor_width, tensor_height])
        return result
